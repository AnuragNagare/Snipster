"""Celery tasks — the async worker functions that do the heavy lifting."""

import json
import time
import uuid
from datetime import datetime, timezone

from app.workers.celery_app import celery_app
from app.core.logging import logger


def _stage_start(name: str, ident: str) -> float:
    """Mark the start of a pipeline stage; pass the result to _stage_done."""
    logger.info(f"[{name}] Starting for {ident}")
    return time.monotonic()


def _stage_done(name: str, ident: str, started: float) -> None:
    """Log how long a stage took, so a slow step is measurable rather than felt."""
    logger.info(f"[STAGE] {name} took {time.monotonic() - started:.1f}s ({ident})")


def _ytdl_format() -> str:
    """Cap the resolution yt-dlp fetches — the render output is only 1080x1920."""
    from app.core.config import settings

    height = settings.MAX_SOURCE_HEIGHT
    if not height:
        return "bestvideo+bestaudio/best"
    return (
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/"
        f"bestvideo+bestaudio/"
        f"best"
    )


def _release_source_if_done(db, source_video_id) -> None:
    """Drop the cached source once no clip still needs it."""
    from app.db.models import Clip, RenderStatus
    from app.services import media_cache

    pending = (
        db.query(Clip)
        .filter(
            Clip.source_video_id == source_video_id,
            Clip.render_status.in_([RenderStatus.PENDING, RenderStatus.RENDERING]),
        )
        .count()
    )
    if pending == 0:
        media_cache.release(str(source_video_id))


_SessionFactory = None


def _get_sync_session():
    """Create a synchronous DB session for use inside Celery tasks."""
    global _SessionFactory
    if _SessionFactory is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.core.config import settings

        # psycopg2 for sync sessions in Celery workers. One engine (and pool) per
        # process — a hosted Postgres has a connection limit, and a new engine per
        # task would leak connections against it.
        engine = create_engine(
            settings.sync_database_url, pool_pre_ping=True, pool_recycle=300
        )
        _SessionFactory = sessionmaker(bind=engine)
    return _SessionFactory()


# ─── Download (YouTube URL) ───────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.workers.tasks.download_video_task", max_retries=2)
def download_video_task(self, video_id: str):
    """Download a YouTube video via yt-dlp, upload to R2, then trigger transcription."""
    from app.db.models import SourceVideo, VideoStatus
    from app.services import media_cache
    from app.services.storage import storage_service
    import tempfile, os, yt_dlp

    started = _stage_start("DOWNLOAD", f"video {video_id}")
    db = _get_sync_session()

    try:
        video = db.get(SourceVideo, uuid.UUID(video_id))
        if not video or not video.youtube_url:
            raise ValueError(f"Video {video_id} has no YouTube URL")

        video.status = VideoStatus.TRANSCRIBING  # reuse status — downloading is fast
        db.commit()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "%(id)s.%(ext)s")
            ydl_opts = {
                "format": _ytdl_format(),
                "outtmpl": output_template,
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                # YouTube serves fragmented formats; pulling fragments in
                # parallel is most of the download win.
                "concurrent_fragment_downloads": 8,
                # Bypass YouTube bot detection by using android/ios/web client emulation
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "ios", "web"],
                    }
                },
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video.youtube_url, download=True)
                downloaded_file = ydl.prepare_filename(info)
                # yt-dlp may merge to .mp4 or .mkv — find the actual file
                for f in os.listdir(tmpdir):
                    if f.startswith(info["id"]):
                        downloaded_file = os.path.join(tmpdir, f)
                        break

            storage_key = f"source/{video_id}/video.mp4"
            storage_service.upload_file(storage_key, downloaded_file, content_type="video/mp4")

            # Keep the bytes we already have on disk — transcription and every
            # render would otherwise pull them straight back out of R2.
            media_cache.put_local_source(video_id, downloaded_file)

        video.storage_key = storage_key
        video.original_filename = info.get("title", "youtube_video") + ".mp4"
        video.duration_seconds = info.get("duration", 0)
        db.commit()

        logger.info(f"[DOWNLOAD] Done for video {video_id}, now transcribing")
        transcribe_video_task.delay(video_id)

    except Exception as exc:
        logger.error(f"[DOWNLOAD] Failed for video {video_id}: {exc}")
        video = db.get(SourceVideo, uuid.UUID(video_id))
        if video:
            video.status = VideoStatus.FAILED
            video.error_message = str(exc)
            db.commit()
        raise self.retry(exc=exc, countdown=60)
    finally:
        _stage_done("DOWNLOAD", f"video {video_id}", started)
        db.close()


# ─── Transcription ────────────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.workers.tasks.transcribe_video_task", max_retries=2)
def transcribe_video_task(self, video_id: str):
    """Transcribe audio from a source video using faster-whisper."""
    from app.db.models import SourceVideo, VideoStatus
    from app.services import media_cache
    from app.services.transcription import transcription_service
    import os

    started = _stage_start("TRANSCRIBE", f"video {video_id}")
    db = _get_sync_session()

    try:
        video = db.get(SourceVideo, uuid.UUID(video_id))
        if not video:
            logger.error(f"[TRANSCRIBE] Video not found: {video_id}")
            return

        video.status = VideoStatus.TRANSCRIBING
        db.commit()

        # Cached after this — the render tasks reuse the same local copy.
        source_path = media_cache.get_local_source(video_id, video.storage_key)
        try:
            transcript_data = transcription_service.transcribe(source_path)
        finally:
            if not media_cache.enabled():
                os.unlink(source_path)  # uncached path is a throwaway temp file

        video.transcript = json.dumps(transcript_data)
        video.status = VideoStatus.SELECTING_CLIPS
        db.commit()

        # Chain to clip selection
        select_clips_task.delay(video_id)

    except Exception as exc:
        logger.error(f"[TRANSCRIBE] Failed for video {video_id}: {exc}")
        video.status = VideoStatus.FAILED
        video.error_message = str(exc)
        db.commit()
        raise self.retry(exc=exc, countdown=60)
    finally:
        _stage_done("TRANSCRIBE", f"video {video_id}", started)
        db.close()


# ─── Clip Selection ───────────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.workers.tasks.select_clips_task", max_retries=2)
def select_clips_task(self, video_id: str):
    """Ask Gemini to identify 3–5 clip-worthy segments from the transcript."""
    from app.db.models import Clip, SourceVideo, VideoStatus
    from app.services.llm import llm_service

    started = _stage_start("SELECT_CLIPS", f"video {video_id}")
    db = _get_sync_session()

    try:
        video = db.get(SourceVideo, uuid.UUID(video_id))
        if not video or not video.transcript:
            raise ValueError("Video transcript not available.")

        transcript = json.loads(video.transcript)
        segments = llm_service.select_clips(transcript)

        for seg in segments:
            clip = Clip(
                source_video_id=uuid.UUID(video_id),
                start_time=seg["start"],
                end_time=seg["end"],
                llm_reason=seg.get("reason", ""),
            )
            db.add(clip)

        db.flush()
        video.status = VideoStatus.READY
        db.commit()

        # Fan-out: render + caption each clip
        for clip in db.query(Clip).filter(Clip.source_video_id == uuid.UUID(video_id)).all():
            render_clip_task.delay(str(clip.id))
            generate_caption_task.delay(str(clip.id))

        logger.info(f"[SELECT_CLIPS] Created {len(segments)} clips for video {video_id}")

    except Exception as exc:
        logger.error(f"[SELECT_CLIPS] Failed for video {video_id}: {exc}")
        raise self.retry(exc=exc, countdown=30)
    finally:
        _stage_done("SELECT_CLIPS", f"video {video_id}", started)
        db.close()


# ─── Rendering ────────────────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.workers.tasks.render_clip_task", max_retries=2)
def render_clip_task(self, clip_id: str):
    """Render a clip: trim → 9:16 crop → burn captions → upload to R2."""
    from app.db.models import Clip, RenderStatus, SourceVideo
    from app.services import media_cache
    from app.services.renderer import renderer_service
    from app.services.storage import storage_service
    import tempfile, os

    started = _stage_start("RENDER", f"clip {clip_id}")
    db = _get_sync_session()

    try:
        clip = db.get(Clip, uuid.UUID(clip_id))
        if not clip:
            raise ValueError(f"Clip not found: {clip_id}")

        clip.render_status = RenderStatus.RENDERING
        db.commit()

        video = db.get(SourceVideo, clip.source_video_id)
        transcript = json.loads(video.transcript) if video.transcript else None

        # One local copy is shared by every clip of this video, so only the
        # first render of a video pays for the transfer.
        source_path = media_cache.get_local_source(str(video.id), video.storage_key)

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = os.path.join(tmpdir, "clip.mp4")

                renderer_service.render(
                    source_path=source_path,
                    output_path=output_path,
                    start=clip.start_time,
                    end=clip.end_time,
                    transcript=transcript,
                )

                storage_key = f"clips/{clip_id}/clip.mp4"
                storage_service.upload_file(storage_key, output_path, content_type="video/mp4")
        finally:
            if not media_cache.enabled():
                os.unlink(source_path)

        clip.storage_key = storage_key
        clip.render_status = RenderStatus.DONE
        db.commit()
        logger.info(f"[RENDER] Done for clip {clip_id}")

        _release_source_if_done(db, clip.source_video_id)

    except Exception as exc:
        logger.error(f"[RENDER] Failed for clip {clip_id}: {exc}")
        clip.render_status = RenderStatus.FAILED
        db.commit()
        raise self.retry(exc=exc, countdown=60)
    finally:
        _stage_done("RENDER", f"clip {clip_id}", started)
        db.close()


# ─── Caption Generation ───────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.workers.tasks.generate_caption_task", max_retries=2)
def generate_caption_task(self, clip_id: str):
    """Generate a caption + hashtags for a clip using Gemini."""
    from app.db.models import Clip
    from app.services.llm import llm_service
    import json

    started = _stage_start("CAPTION", f"clip {clip_id}")
    db = _get_sync_session()

    try:
        clip = db.get(Clip, uuid.UUID(clip_id))
        if not clip:
            raise ValueError(f"Clip not found: {clip_id}")

        video = clip.source_video
        transcript = json.loads(video.transcript) if video.transcript else []

        result = llm_service.generate_caption(
            transcript=transcript,
            start=clip.start_time,
            end=clip.end_time,
            reason=clip.llm_reason or "",
        )

        clip.caption = result.get("caption", "")
        clip.hashtags = result.get("hashtags", "")
        db.commit()
        logger.info(f"[CAPTION] Done for clip {clip_id}")

    except Exception as exc:
        logger.error(f"[CAPTION] Failed for clip {clip_id}: {exc}")
        raise self.retry(exc=exc, countdown=30)
    finally:
        _stage_done("CAPTION", f"clip {clip_id}", started)
        db.close()


# ─── Publishing ───────────────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.workers.tasks.publish_clip_task", max_retries=3)
def publish_clip_task(self, job_id: str):
    """Publish a clip to its target platform (YouTube or Instagram)."""
    from app.db.models import Clip, Platform, PublishJob, PublishStatus, User
    from app.services.youtube import youtube_service
    from app.services.instagram import instagram_service
    from sqlalchemy import select

    logger.info(f"[PUBLISH] Starting job {job_id}")
    db = _get_sync_session()

    try:
        job = db.get(PublishJob, uuid.UUID(job_id))
        if not job:
            raise ValueError(f"PublishJob not found: {job_id}")

        job.status = PublishStatus.PUBLISHING
        db.commit()

        clip = job.clip
        user = db.query(User).first()

        if job.platform == Platform.YOUTUBE:
            result = youtube_service.upload(clip=clip, user=user)
        elif job.platform == Platform.INSTAGRAM:
            result = instagram_service.publish(clip=clip, user=user)
        else:
            raise ValueError(f"Unknown platform: {job.platform}")

        job.status = PublishStatus.PUBLISHED
        job.platform_post_id = result.get("post_id", "")
        job.platform_post_url = result.get("post_url", "")
        job.published_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(f"[PUBLISH] Success: job {job_id} → {job.platform_post_url}")

    except Exception as exc:
        logger.error(f"[PUBLISH] Failed: job {job_id}: {exc}")
        job.status = PublishStatus.FAILED
        job.error_info = json.dumps({"error": str(exc), "retry": self.request.retries})
        db.commit()
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
    finally:
        db.close()
