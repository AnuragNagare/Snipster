"""Autopilot: on a schedule, pick an unused video from the user's OWN channel and send it down the pipeline.

Runs inside the Celery worker (synchronous SQLAlchemy). Nothing here talks to a channel other than
the one the user connected: the uploads list is derived from that channel's id.

Source policy: own uploads only. Other creators' videos are never picked.
"""

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import logger
from app.db.models import (
    ApprovalStatus,
    AutopilotSettings,
    AutopilotSource,
    AutopilotSourceStatus,
    Clip,
    Platform,
    PublishJob,
    PublishStatus,
    RenderStatus,
    SourceVideo,
    User,
    VideoStatus,
)
from app.services import alerts

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

# A picked video whose pipeline has not finished after this long is given up on, so one lost task
# cannot block autopilot forever.
STALE_AFTER = timedelta(hours=12)


class AutopilotConfigError(Exception):
    """Autopilot cannot run until the user fixes a setting (API key missing, YouTube not connected, ...)."""


# ─── Settings ────────────────────────────────────────────────────────────────

def get_settings_row(db: Session) -> AutopilotSettings:
    """The single settings row, created with the safe defaults (off, dry run, private) on first use."""
    row = db.get(AutopilotSettings, 1)
    if row is None:
        row = AutopilotSettings(
            id=1, enabled=False, dry_run=True, auto_approve=False, posts_per_day=3,
            clips_per_video=2, run_times="9,18", timezone="UTC", privacy="private",
        )
        db.add(row)
        db.commit()
    return row


def parse_run_hours(text: str | None) -> list[int]:
    """'9, 18' -> [9, 18]. Anything that is not an hour 0-23 is ignored."""
    hours = set()
    for part in (text or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 23:
            hours.add(int(part))
    return sorted(hours)


def tz_of(row: AutopilotSettings) -> ZoneInfo:
    """The user's timezone, or UTC if the stored name is not a real one."""
    try:
        return ZoneInfo(row.timezone or "UTC")
    except ZoneInfoNotFoundError:
        logger.warning(f"[AUTOPILOT] Unknown timezone {row.timezone!r}, using UTC")
        return ZoneInfo("UTC")


def latest_due_slot(row: AutopilotSettings, now: datetime) -> str | None:
    """
    The most recent run slot (like "2026-11-15T09") that has begun today in the user's timezone.

    Only the latest one counts: if the PC was off through two slots, autopilot runs once when it is
    back instead of firing a burst of catch-up runs.
    """
    local = now.astimezone(tz_of(row))
    started = [h for h in parse_run_hours(row.run_times) if h <= local.hour]
    if not started:
        return None
    return f"{local:%Y-%m-%d}T{max(started):02d}"


# ─── Reading the channel ─────────────────────────────────────────────────────

_ISO_DURATION = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def parse_iso_duration(text: str) -> float:
    """'PT1H2M3S' -> 3723.0. Unparseable -> 0."""
    m = _ISO_DURATION.match(text or "")
    if not m:
        return 0.0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + s)


def uploads_playlist_id(channel_id: str) -> str:
    """Every channel's uploads are a playlist whose id is the channel id with 'UC' swapped for 'UU'."""
    if not channel_id.startswith("UC"):
        raise AutopilotConfigError(f"Unexpected channel id {channel_id!r}. Reconnect YouTube in Settings.")
    return "UU" + channel_id[2:]


def _get(path: str, params: dict) -> dict:
    response = httpx.get(f"{YOUTUBE_API}/{path}", params={**params, "key": settings.YOUTUBE_API_KEY}, timeout=30.0)
    if response.status_code == 404:
        return {"items": []}
    if response.status_code != 200:
        raise RuntimeError(f"YouTube API {path} failed: {response.status_code} {response.text[:300]}")
    return response.json()


def fetch_uploads(channel_id: str, max_pages: int | None = None) -> list[dict]:
    """
    Public uploads of the channel, as dicts: id, title, published_at, duration, live, public.
    Costs about 1 quota unit per 50 videos for the list plus 1 per 50 for the details.
    """
    playlist = uploads_playlist_id(channel_id)
    pages = max_pages or settings.AUTOPILOT_MAX_PAGES

    ids: list[str] = []
    meta: dict[str, dict] = {}
    token = None
    for _ in range(pages):
        params = {"part": "snippet,contentDetails", "playlistId": playlist, "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = _get("playlistItems", params)
        for item in data.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid and vid not in meta:
                ids.append(vid)
                meta[vid] = {
                    "id": vid,
                    "title": item.get("snippet", {}).get("title", ""),
                    "published_at": item.get("contentDetails", {}).get("videoPublishedAt")
                    or item.get("snippet", {}).get("publishedAt", ""),
                }
        token = data.get("nextPageToken")
        if not token:
            break

    uploads = []
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        data = _get("videos", {"part": "contentDetails,snippet,status", "id": ",".join(batch), "maxResults": 50})
        for item in data.get("items", []):
            info = meta.get(item["id"])
            if not info:
                continue
            info["duration"] = parse_iso_duration(item.get("contentDetails", {}).get("duration", ""))
            info["live"] = item.get("snippet", {}).get("liveBroadcastContent", "none") != "none"
            info["public"] = item.get("status", {}).get("privacyStatus") == "public"
            uploads.append(info)
    return uploads


def select_candidate(uploads: list[dict], used_ids: set[str]) -> dict | None:
    """The oldest unused upload that is public, not live, and within the allowed length."""
    lo, hi = settings.AUTOPILOT_MIN_DURATION, settings.AUTOPILOT_MAX_DURATION
    eligible = [
        u for u in uploads
        if u["id"] not in used_ids
        and u.get("public")
        and not u.get("live")
        and lo <= u.get("duration", 0) <= hi
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda u: u["published_at"])


# ─── Starting a source ───────────────────────────────────────────────────────

def start_next_source(db: Session, user: User | None, *, fetch=fetch_uploads) -> AutopilotSource | None:
    """
    Pick the next unused upload and start the pipeline for it. Returns the new source row,
    or None when the channel has nothing eligible left.

    Raises AutopilotConfigError if autopilot is not set up.
    """
    if not settings.YOUTUBE_API_KEY:
        raise AutopilotConfigError("YOUTUBE_API_KEY is not set in the worker's backend/.env.")
    if user is None or not user.yt_channel_id:
        raise AutopilotConfigError("YouTube is not connected. Connect it in Settings.")

    used = set(db.scalars(select(AutopilotSource.youtube_video_id)).all())
    candidate = select_candidate(fetch(user.yt_channel_id), used)
    if candidate is None:
        logger.info("[AUTOPILOT] No unused eligible video on the channel")
        return None

    source = AutopilotSource(
        youtube_video_id=candidate["id"],
        title=candidate["title"][:512],
        duration_seconds=candidate["duration"],
        status=AutopilotSourceStatus.PICKED,
    )
    video = SourceVideo(
        user_id=user.id,
        youtube_url=f"https://www.youtube.com/watch?v={candidate['id']}",
        ownership_confirmed=True,  # it comes from the connected user's own channel
        status=VideoStatus.PENDING,
    )
    try:
        db.add(source)
        db.add(video)
        db.flush()
        source.source_video_id = video.id
        source.status = AutopilotSourceStatus.PROCESSING
        db.commit()  # before queueing: the worker looks the video up by id
    except IntegrityError:
        db.rollback()  # another tick took this video first
        logger.info(f"[AUTOPILOT] {candidate['id']} was already picked")
        return None

    # Imported here: the pipeline module imports the Celery tasks, which import this module.
    from app.workers.pipeline import start_processing_pipeline

    try:
        start_processing_pipeline(str(video.id), from_url=True)
    except Exception:
        # The queue is unreachable. Forget the pick so the next tick retries the same video
        # instead of burning through the channel one failure at a time.
        db.delete(source)
        db.delete(video)
        db.commit()
        raise

    logger.info(f"[AUTOPILOT] Picked {candidate['id']} ({candidate['title']!r}) -> video {video.id}")
    return source


def in_flight(db: Session) -> bool:
    return db.scalar(
        select(AutopilotSource.id).where(AutopilotSource.status.in_(AutopilotSourceStatus.IN_FLIGHT)).limit(1)
    ) is not None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sync_source_statuses(db: Session, now: datetime, notify=alerts.send) -> list[AutopilotSource]:
    """
    Mark picked videos failed when their pipeline failed or has been stuck too long, and tell the user.
    Returns the rows that just failed.
    """
    rows = db.scalars(
        select(AutopilotSource).where(AutopilotSource.status.in_(AutopilotSourceStatus.IN_FLIGHT))
    ).all()
    failed = []
    for row in rows:
        video = db.get(SourceVideo, row.source_video_id) if row.source_video_id else None
        if video is None or video.status == VideoStatus.FAILED:
            row.status = AutopilotSourceStatus.FAILED
            row.reason = (video.error_message if video else None) or "The video record is gone."
        elif video.status != VideoStatus.READY and now - _aware(row.picked_at) > STALE_AFTER:
            row.status = AutopilotSourceStatus.FAILED
            row.reason = "Timed out: the pipeline did not finish. Is the worker running?"
        else:
            continue
        failed.append(row)
    db.commit()
    for row in failed:
        notify(f"Video \"{row.title}\" could not be processed: {(row.reason or '')[:300]}")
    return failed


# ─── Deciding which clips go out (A5) ────────────────────────────────────────

MIN_POST_SECONDS = 15
MAX_POST_SECONDS = 60
TARGET_POST_SECONDS = 40   # no quality score exists, so prefer clips close to a good Shorts length
MAX_OVERLAP = 0.5          # two chosen clips may not share more than this much of the shorter one

# How long to wait, after the video reaches READY, for every clip to be rendered and captioned.
# A render or caption task that fails for good never finishes, and must not block autopilot forever.
CLIP_PATIENCE = timedelta(hours=2)


def check_clip(clip: Clip) -> str | None:
    """Why this clip must not be posted automatically, or None if it passes."""
    if clip.render_status != RenderStatus.DONE:
        return "the render did not finish"
    if not clip.storage_key:
        return "no rendered file"
    if not (clip.caption or "").strip():
        return "no caption"
    length = clip.end_time - clip.start_time
    if length < MIN_POST_SECONDS:
        return f"too short ({length:.0f}s)"
    if length > MAX_POST_SECONDS:
        return f"too long ({length:.0f}s)"
    return None


def _overlap(a: Clip, b: Clip) -> float:
    """Shared time as a fraction of the shorter clip."""
    shared = min(a.end_time, b.end_time) - max(a.start_time, b.start_time)
    shorter = min(a.end_time - a.start_time, b.end_time - b.start_time)
    return max(0.0, shared) / shorter if shorter > 0 else 1.0


def choose_clips(clips: list[Clip], count: int) -> tuple[list[Clip], dict]:
    """
    The clips to post (at most `count`) and, for every other clip, why it was left out.

    Clips that fail `check_clip` are out. Of the rest, those closest to TARGET_POST_SECONDS come first,
    and a clip that mostly repeats an already chosen one is skipped.
    """
    reasons: dict = {}
    passing = []
    for clip in clips:
        problem = check_clip(clip)
        if problem:
            reasons[clip.id] = problem
        else:
            passing.append(clip)
    passing.sort(key=lambda c: (abs((c.end_time - c.start_time) - TARGET_POST_SECONDS), c.start_time))

    chosen: list[Clip] = []
    for clip in passing:
        if len(chosen) >= count:
            reasons[clip.id] = f"not among the best {count}"
        elif any(_overlap(clip, other) > MAX_OVERLAP for other in chosen):
            reasons[clip.id] = "overlaps a clip that was chosen"
        else:
            chosen.append(clip)
    return chosen, reasons


def plan_post_times(
    now: datetime,
    tz: ZoneInfo,
    count: int,
    existing: list[datetime],
    posts_per_day: int,
    first_hour: int = 9,
) -> list[datetime]:
    """
    When to post `count` new clips: spread out (24h / posts_per_day apart) and never more than
    `posts_per_day` on any calendar day in the user's timezone, counting posts already planned or made
    (`existing`). A full day pushes the rest to the next morning at `first_hour`.
    """
    spacing = timedelta(hours=24 / max(1, posts_per_day))
    taken = list(existing)
    candidate = now + timedelta(minutes=5)
    recent = [t for t in taken if t >= now - spacing]
    if recent:
        candidate = max(candidate, max(recent) + spacing)

    times = []
    for _ in range(count):
        while True:
            local = candidate.astimezone(tz)
            if local.hour < first_hour:  # spacing can land in the small hours: wait for the morning
                candidate = datetime.combine(local.date(), time(first_hour), tzinfo=tz).astimezone(timezone.utc)
            day = candidate.astimezone(tz).date()
            if sum(1 for t in taken if t.astimezone(tz).date() == day) < posts_per_day:
                break
            next_morning = datetime.combine(day + timedelta(days=1), time(first_hour), tzinfo=tz)
            candidate = next_morning.astimezone(timezone.utc)
        times.append(candidate)
        taken.append(candidate)
        candidate = candidate + spacing
    return times


def _existing_post_times(db: Session, now: datetime) -> list[datetime]:
    """When non-failed posts are, or were, due: they count against the daily limit."""
    since = now - timedelta(days=3)
    rows = db.execute(
        select(PublishJob.published_at, PublishJob.scheduled_at, PublishJob.created_at)
        .where(PublishJob.status != PublishStatus.FAILED, PublishJob.created_at >= since)
    ).all()
    return [_aware(p or s or c) for p, s, c in rows if (p or s or c)]


def _is_settled(clip: Clip) -> bool:
    return clip.render_status == RenderStatus.DONE and bool((clip.caption or "").strip())


def finalize_ready_sources(db: Session, row: AutopilotSettings, now: datetime, notify=alerts.send) -> list[str]:
    """
    For each picked video whose clips are ready: approve the best ones and schedule their posts.

    Waits until every clip is rendered and captioned (or CLIP_PATIENCE has passed). With auto-approve
    off, it only marks the source done and leaves every clip for the user to review.
    Returns a short note per source it finished.
    """
    notes = []
    sources = db.scalars(
        select(AutopilotSource).where(AutopilotSource.status == AutopilotSourceStatus.PROCESSING)
    ).all()
    for src in sources:
        video = db.get(SourceVideo, src.source_video_id) if src.source_video_id else None
        if video is None or video.status != VideoStatus.READY:
            continue
        clips = db.scalars(select(Clip).where(Clip.source_video_id == video.id)).all()
        if not clips:
            src.status = AutopilotSourceStatus.FAILED
            src.reason = "The AI found no clips in this video."
            db.commit()
            notify(f"Video \"{src.title}\": no clips were found in it.")
            continue

        ready_since = _aware(video.updated_at or src.picked_at)
        if not all(_is_settled(c) for c in clips) and now - ready_since < CLIP_PATIENCE:
            continue  # still rendering or captioning

        if not row.auto_approve:
            src.status = AutopilotSourceStatus.DONE
            src.reason = "Clips are waiting for your review (auto-approve is off)."
            db.commit()
            notes.append(f"{src.youtube_video_id}: left for review")
            continue

        chosen, reasons = choose_clips(clips, row.clips_per_video)
        if not chosen:
            summary = "; ".join(sorted(set(reasons.values()))) or "no clips"
            src.status = AutopilotSourceStatus.FAILED
            src.reason = f"No clip passed the checks: {summary}."
            db.commit()
            notify(f"Video \"{src.title}\": no clip passed the checks ({summary}).")
            continue

        already = {
            j for j in db.scalars(
                select(PublishJob.clip_id).where(
                    PublishJob.clip_id.in_([c.id for c in chosen]), PublishJob.status != PublishStatus.FAILED
                )
            ).all()
        }
        chosen = [c for c in chosen if c.id not in already]  # never queue a clip twice
        tz = tz_of(row)
        first_hour = (parse_run_hours(row.run_times) or [9])[0]
        times = plan_post_times(now, tz, len(chosen), _existing_post_times(db, now), row.posts_per_day, first_hour)

        for clip, when in zip(chosen, times):
            clip.approval_status = ApprovalStatus.APPROVED
            db.add(PublishJob(
                clip_id=clip.id, platform=Platform.YOUTUBE, status=PublishStatus.SCHEDULED,
                scheduled_at=when, retry_count=0, autopilot=True,
            ))
        for clip in clips:
            if clip not in chosen and clip.approval_status == ApprovalStatus.PENDING and clip.id not in already:
                clip.approval_status = ApprovalStatus.REJECTED

        left_out = sorted(set(reasons.values()))
        src.status = AutopilotSourceStatus.DONE
        src.reason = f"Scheduled {len(chosen)} post(s)." + (f" Left out: {'; '.join(left_out)}." if left_out else "")
        db.commit()
        notes.append(f"{src.youtube_video_id}: scheduled {len(chosen)}")
        logger.info(f"[AUTOPILOT] {src.youtube_video_id}: {src.reason}")
    return notes


# ─── Dispatcher for scheduled posts ──────────────────────────────────────────

def dispatch_due_publish_jobs(db: Session, now: datetime, publish, include_autopilot: bool = True) -> int:
    """
    Queue every SCHEDULED publish job whose time has come. Returns how many were queued.

    Each job is claimed with one conditional UPDATE (SCHEDULED -> QUEUED), so two overlapping
    ticks cannot both queue the same post. With `include_autopilot=False` (the kill switch is off)
    only posts a person scheduled by hand go out.
    """
    query = select(PublishJob.id).where(PublishJob.status == PublishStatus.SCHEDULED, PublishJob.scheduled_at <= now)
    if not include_autopilot:
        query = query.where(PublishJob.autopilot.is_(False))
    due = db.scalars(query).all()
    queued = 0
    for job_id in due:
        claimed = db.execute(
            update(PublishJob)
            .where(PublishJob.id == job_id, PublishJob.status == PublishStatus.SCHEDULED)
            .values(status=PublishStatus.QUEUED)
        ).rowcount
        db.commit()  # the task looks the job up by id
        if claimed != 1:
            continue
        try:
            publish.delay(str(job_id))
            queued += 1
        except Exception as e:
            db.execute(update(PublishJob).where(PublishJob.id == job_id).values(status=PublishStatus.SCHEDULED))
            db.commit()
            logger.error(f"[AUTOPILOT] Could not queue publish job {job_id}, will retry next tick: {e}")
    return queued


# ─── The tick ────────────────────────────────────────────────────────────────

def run_tick(
    db: Session,
    *,
    publish,
    now: datetime | None = None,
    start=start_next_source,
    notify=alerts.send,
    force: bool = False,
) -> dict:
    """
    One scheduler wake-up. Safe to run any number of times: every step checks the database first.

    1. Queue scheduled posts that are due. Posts a person scheduled by hand always go out; posts autopilot
       scheduled only go out while autopilot is enabled (the kill switch).
    2. Give up on picked videos that failed or stalled, and tell the user.
    3. If autopilot is on: approve and schedule clips of videos that are ready, then, when a run slot has
       begun that has not run yet (or `force`), pick the next video unless one is still in progress.
    """
    now = now or datetime.now(timezone.utc)
    row = get_settings_row(db)
    summary: dict = {"dispatched": 0, "picked": None, "scheduled": [], "note": None}

    summary["dispatched"] = dispatch_due_publish_jobs(db, now, publish, include_autopilot=row.enabled)
    sync_source_statuses(db, now, notify)

    if row.enabled:
        summary["scheduled"] = finalize_ready_sources(db, row, now, notify)

        slot = latest_due_slot(row, now)
        if not force and (slot is None or slot == row.last_run_slot):
            summary["note"] = "no run slot due"
        elif in_flight(db):
            summary["note"] = "previous video still in progress"
        else:
            try:
                picked = start(db, db.query(User).first())
            except AutopilotConfigError as e:
                summary["note"] = f"not set up: {e}"
                logger.warning(f"[AUTOPILOT] {summary['note']}")
                notify(f"Autopilot cannot start a video: {e}", key="config", cooldown=6 * 3600)
            except Exception as e:  # the YouTube API or the queue is down: try again next tick
                db.rollback()
                summary["note"] = f"error: {e}"
                logger.error(f"[AUTOPILOT] Could not start a video: {e}")
                notify(f"Autopilot could not start a video: {str(e)[:300]}", key="start-error", cooldown=3600)
            else:
                if not force:
                    row.last_run_slot = slot  # used up even if nothing was eligible
                summary["picked"] = picked.youtube_video_id if picked else None
                summary["note"] = None if picked else "no unused eligible video"
                if not picked:
                    notify("Autopilot found no unused video on your channel.", key="empty", cooldown=24 * 3600)

    row.last_tick_at = now
    db.commit()
    return summary
