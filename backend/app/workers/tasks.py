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
            transcript_data, language = transcription_service.transcribe(source_path)
        finally:
            if not media_cache.enabled():
                os.unlink(source_path)  # uncached path is a throwaway temp file

        from app.core.config import settings

        if len(transcript_data) < settings.MIN_TRANSCRIPT_WORDS:
            # Nothing to clip. Retrying cannot help, and asking the AI to pick clips from an empty
            # transcript would make it invent them.
            video.transcript = json.dumps(transcript_data)
            video.language = language
            video.status = VideoStatus.FAILED
            video.error_message = (
                f"Almost no speech in this video ({len(transcript_data)} words found, at least "
                f"{settings.MIN_TRANSCRIPT_WORDS} needed), so there is nothing to clip."
            )
            db.commit()
            logger.warning(f"[TRANSCRIBE] {video_id}: {video.error_message}")
            return

        video.transcript = json.dumps(transcript_data)
        video.language = language
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

        from app.core.config import settings

        if len(transcript) < settings.MIN_TRANSCRIPT_WORDS:
            # Also checked here, not only after transcription: a task that is delivered again must never
            # ask the AI to pick clips from an (almost) empty transcript.
            video.status = VideoStatus.FAILED
            video.error_message = (
                f"Almost no speech in this video ({len(transcript)} words found, at least "
                f"{settings.MIN_TRANSCRIPT_WORDS} needed), so there is nothing to clip."
            )
            db.commit()
            logger.warning(f"[SELECT_CLIPS] {video_id}: {video.error_message}")
            return

        segments = llm_service.select_clips(transcript, language=video.language)

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
                    language=video.language,
                    subtitles=json.loads(clip.subtitles) if clip.subtitles is not None else None,
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
            language=video.language,
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

DRY_RUN_POST_ID = "dry-run"


def _save_published(db, job, post_id: str, post_url) -> None:
    """Record a finished post. Tried three times: the video is already on YouTube at this point."""
    from app.db.models import PublishStatus

    for attempt in range(3):
        try:
            job.status = PublishStatus.PUBLISHED
            job.platform_post_id = post_id
            job.platform_post_url = post_url
            job.published_at = datetime.now(timezone.utc)
            db.commit()
            return
        except Exception:
            db.rollback()
            if attempt == 2:
                raise
            time.sleep(2)


@celery_app.task(bind=True, name="app.workers.tasks.publish_clip_task", max_retries=3)
def publish_clip_task(self, job_id: str):
    """Publish a clip to YouTube."""
    from app.db.models import AutopilotSettings, AutopilotSource, Clip, Platform, PublishJob, PublishStatus, User
    from app.services import alerts
    from app.services.autopilot import credit_line
    from app.services.youtube import YouTubeAuthError, youtube_service
    from sqlalchemy import select

    logger.info(f"[PUBLISH] Starting job {job_id}")
    db = _get_sync_session()
    job = None
    uploaded = None  # the YouTube video id, once the upload has succeeded

    def label() -> str:
        caption = (job.clip.caption or "").strip() if job is not None and job.clip else ""
        return f"\"{caption[:60]}\"" if caption else f"job {job_id}"

    try:
        job = db.get(PublishJob, uuid.UUID(job_id))
        if not job:
            raise ValueError(f"PublishJob not found: {job_id}")

        # Already on YouTube (a retry after a failed save): never upload the same clip twice.
        if job.platform_post_id and job.platform_post_id != DRY_RUN_POST_ID:
            job.status = PublishStatus.PUBLISHED
            db.commit()
            logger.warning(f"[PUBLISH] Job {job_id} already has post {job.platform_post_id}, not uploading again")
            return

        # Once the autopilot settings row exists its privacy applies to every upload;
        # before that, uploads stay public like they always were.
        row = db.get(AutopilotSettings, 1)

        # Dry run applies to autopilot's own posts only: a post you start yourself always uploads.
        if job.autopilot and row is not None and row.dry_run:
            _save_published(db, job, DRY_RUN_POST_ID, None)
            logger.info(f"[PUBLISH] DRY RUN: job {job_id} ({label()}) would have been uploaded")
            return

        job.status = PublishStatus.PUBLISHING
        db.commit()

        clip = job.clip
        user = db.query(User).first()
        privacy = row.privacy if row else "public"

        # A Creative Commons source must be credited in the description of every post made from it.
        source = db.query(AutopilotSource).filter(AutopilotSource.source_video_id == clip.source_video_id).first()
        credit = credit_line(source)

        if job.platform == Platform.YOUTUBE:
            result = youtube_service.upload(clip=clip, user=user, privacy=privacy, credit=credit)
        else:
            raise ValueError(f"Unknown platform: {job.platform}")

        uploaded = result.get("post_id", "")
        logger.info(f"[PUBLISH] Uploaded video {uploaded} for job {job_id}")
        _save_published(db, job, uploaded, result.get("post_url", ""))
        logger.info(f"[PUBLISH] Success: job {job_id} → {job.platform_post_url}")

    except YouTubeAuthError as exc:
        # Reconnecting is the only fix, so do not retry.
        logger.error(f"[PUBLISH] Sign-in problem, job {job_id} not retried: {exc}")
        db.rollback()
        if job is not None:
            job.status = PublishStatus.FAILED
            job.error_info = json.dumps({"error": str(exc), "retry": self.request.retries, "auth": True})
            db.commit()
        alerts.send(f"Could not post {label()}: {exc}", key="auth", cooldown=3600)
    except Exception as exc:
        if uploaded:
            # The video IS on YouTube; only saving it failed. Retrying would post it a second time.
            url = f"https://www.youtube.com/shorts/{uploaded}"
            logger.critical(f"[PUBLISH] Job {job_id} uploaded as {url} but could not be saved: {exc}")
            try:
                db.rollback()
                job.status = PublishStatus.FAILED
                job.error_info = json.dumps({"error": f"Uploaded as {url} but not saved. Do NOT retry.", "uploaded": uploaded})
                db.commit()
            except Exception:
                pass
            alerts.send(f"{label()} is on YouTube ({url}) but Snipster could not save that. Do not retry the job.")
            return
        logger.error(f"[PUBLISH] Failed: job {job_id}: {exc}")
        db.rollback()
        if job is not None:
            job.status = PublishStatus.FAILED
            job.error_info = json.dumps({"error": str(exc), "retry": self.request.retries})
            db.commit()
        if self.request.retries >= self.max_retries:
            alerts.send(f"Posting {label()} failed after {self.max_retries + 1} tries: {str(exc)[:300]}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
    finally:
        db.close()


# ─── Autopilot ────────────────────────────────────────────────────────────────

@celery_app.task(name="app.workers.tasks.autopilot_tick_task", ignore_result=True)
def autopilot_tick_task(force: bool = False):
    """
    Run by Celery beat every few minutes (and by "Run now" with force=True). Queues due scheduled
    posts, approves finished videos' best clips and, when autopilot is on, starts the next video from
    the user's channel. Pings the dead-man's switch after a healthy tick. Never raises: a failed tick
    is logged, reported (at most once an hour) and the next one tries again.
    """
    from app.services import alerts, autopilot

    db = _get_sync_session()
    try:
        summary = autopilot.run_tick(db, publish=publish_clip_task, force=force)
        if summary["dispatched"] or summary["picked"] or summary["scheduled"]:
            logger.info(f"[AUTOPILOT] Tick: {summary}")
        else:
            logger.debug(f"[AUTOPILOT] Tick: {summary}")
        alerts.ping_healthcheck()  # only after a tick that ran to the end
    except Exception as exc:
        db.rollback()
        logger.error(f"[AUTOPILOT] Tick failed: {exc}")
        alerts.send(f"Autopilot tick failed: {str(exc)[:300]}", key="tick-error", cooldown=3600)
    finally:
        db.close()
