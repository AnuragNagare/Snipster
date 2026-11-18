"""Autopilot: on a schedule, pick an unused video from the user's OWN channel and send it down the pipeline.

Runs inside the Celery worker (synchronous SQLAlchemy). Nothing here talks to a channel other than
the one the user connected: the uploads list is derived from that channel's id.

Source policy: own uploads only. Other creators' videos are never picked.
"""

import json
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, or_, select, update
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

# With YouTube scheduling on, a post is uploaded up to this long before its time.
EARLY_UPLOAD = timedelta(hours=3)


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
            source_mode="own", source_language="en", post_times="",
            schedule_on_youtube=False, auto_retry=True,
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


# ─── Creative Commons discovery ───────────────────────────────────────────────

CC_LICENSE = "creativeCommon"
SOURCE_MODES = ("own", "creative_commons")


def parse_post_times(text: str | None) -> list[time]:
    """'19:45, 12:15' -> [12:15, 19:45]. Anything that is not a valid HH:MM is ignored."""
    found = set()
    for part in (text or "").split(","):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", part)
        if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
            found.add(time(int(m.group(1)), int(m.group(2))))
    return sorted(found)


def fetch_cc_candidates(language: str, *, now: datetime | None = None) -> list[dict]:
    """
    Recent, popular videos whose owners licensed them for reuse (Creative Commons), with their numbers.

    Two searches (4-20 min and over 20 min), 100 quota units each, then one cheap details call per 50
    videos. The licence is read again from the video's own record: the search filter is only a hint.
    """
    now = now or datetime.now(timezone.utc)
    after = (now - timedelta(days=settings.AUTOPILOT_CC_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    ids: list[str] = []
    for bucket in ("medium", "long"):
        params = {
            "part": "id", "type": "video", "videoLicense": CC_LICENSE, "order": "viewCount",
            "publishedAfter": after, "maxResults": 50, "videoDuration": bucket, "relevanceLanguage": language,
        }
        if language == "hi":
            params["regionCode"] = "IN"
        for item in _get("search", params).get("items", []):
            vid = item.get("id", {}).get("videoId")
            if vid and vid not in ids:
                ids.append(vid)

    found = []
    for i in range(0, len(ids), 50):
        data = _get("videos", {"part": "snippet,contentDetails,statistics,status", "id": ",".join(ids[i:i + 50]), "maxResults": 50})
        for item in data.get("items", []):
            snip, stats, status = item.get("snippet", {}), item.get("statistics", {}), item.get("status", {})
            found.append({
                "id": item["id"],
                "title": snip.get("title", ""),
                "published_at": snip.get("publishedAt", ""),
                "duration": parse_iso_duration(item.get("contentDetails", {}).get("duration", "")),
                "live": snip.get("liveBroadcastContent", "none") != "none",
                "public": status.get("privacyStatus") == "public",
                "license": status.get("license", ""),
                "made_for_kids": bool(status.get("madeForKids", False)),
                "channel_id": snip.get("channelId", ""),
                "channel_title": snip.get("channelTitle", ""),
                "category": str(snip.get("categoryId", "")),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "language": (snip.get("defaultAudioLanguage") or snip.get("defaultLanguage") or "").lower(),
            })
    return found


def cc_score(video: dict) -> int:
    """Popularity: views, with each like counting as much as 50 views (likes show people really liked it)."""
    return video.get("views", 0) + 50 * video.get("likes", 0)


def select_cc_candidate(candidates: list[dict], used_ids: set[str], *, own_channel_id: str = "", language: str = "en") -> dict | None:
    """
    The most popular unused video that is really Creative Commons, public, not live, not made for kids,
    within the length limits, popular enough, in the wanted language, and not from the user's own channel.
    """
    lo, hi = settings.AUTOPILOT_MIN_DURATION, settings.AUTOPILOT_MAX_DURATION
    blocked = {c.strip() for c in settings.AUTOPILOT_CC_BLOCK_CATEGORIES.split(",") if c.strip()}
    eligible = [
        v for v in candidates
        if v.get("license") == CC_LICENSE
        and v.get("category", "") not in blocked
        and v["id"] not in used_ids
        and v.get("public")
        and not v.get("live")
        and not v.get("made_for_kids")
        and lo <= v.get("duration", 0) <= hi
        and v.get("views", 0) >= settings.AUTOPILOT_CC_MIN_VIEWS
        and (not own_channel_id or v.get("channel_id") != own_channel_id)
        and (not v.get("language") or v["language"].startswith(language))
    ]
    return max(eligible, key=cc_score) if eligible else None


def credit_line(source: AutopilotSource | None) -> str | None:
    """The credit every post made from a Creative Commons video must carry in its description."""
    if source is None or source.license != CC_LICENSE:
        return None
    return (
        f'Original video: "{source.title}" by {source.channel_title or "the creator"}\n'
        f"https://www.youtube.com/watch?v={source.youtube_video_id}\n"
        "Licensed under Creative Commons Attribution (CC BY). This Short is an edited clip of that video."
    )


# ─── Starting a source ───────────────────────────────────────────────────────

def start_next_source(
    db: Session, user: User | None, *, fetch=fetch_uploads, fetch_cc=fetch_cc_candidates
) -> AutopilotSource | None:
    """
    Pick the next video and start the pipeline for it. Returns the new source row, or None when
    nothing eligible is left.

    Depending on the settings the video comes from the user's own channel, or is the most popular
    recent Creative Commons video (credited in every post). It is never any other creator's video.

    Raises AutopilotConfigError if autopilot is not set up.
    """
    if not settings.YOUTUBE_API_KEY:
        raise AutopilotConfigError("YOUTUBE_API_KEY is not set in the worker's backend/.env.")

    row = get_settings_row(db)
    mode = row.source_mode if row.source_mode in SOURCE_MODES else "own"
    own_channel = ((user.yt_channel_id if user else "") or settings.YOUTUBE_CHANNEL_ID or "").strip()
    used = set(db.scalars(select(AutopilotSource.youtube_video_id)).all())

    if mode == "creative_commons":
        language = row.source_language if row.source_language in ("en", "hi") else "en"
        candidate = select_cc_candidate(fetch_cc(language), used, own_channel_id=own_channel, language=language)
        if candidate is None:
            logger.info("[AUTOPILOT] No unused eligible Creative Commons video")
            return None
    else:
        if not own_channel:
            raise AutopilotConfigError(
                "The YouTube channel is unknown. Set YOUTUBE_CHANNEL_ID in the worker's backend/.env "
                "(or reconnect YouTube in Settings)."
            )
        candidate = select_candidate(fetch(own_channel), used)
        if candidate is None:
            logger.info("[AUTOPILOT] No unused eligible video on the channel")
            return None

    source = AutopilotSource(
        youtube_video_id=candidate["id"],
        title=candidate["title"][:512],
        duration_seconds=candidate["duration"],
        status=AutopilotSourceStatus.PICKED,
    )
    if mode == "creative_commons":
        source.channel_id = candidate.get("channel_id")
        source.channel_title = (candidate.get("channel_title") or "")[:256]
        source.license = CC_LICENSE
        source.views = candidate.get("views")
        source.likes = candidate.get("likes")
    video = SourceVideo(
        user_id=user.id if user else None,
        youtube_url=f"https://www.youtube.com/watch?v={candidate['id']}",
        # True only for the user's own channel. A Creative Commons video is licensed, not owned.
        ownership_confirmed=(mode == "own"),
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

    logger.info(f"[AUTOPILOT] Picked {candidate['id']} ({candidate['title']!r}, {mode}) -> video {video.id}")
    return source


def waiting_autopilot_posts(db: Session) -> int:
    """Autopilot's own posts that are scheduled or on their way out."""
    return db.scalar(
        select(func.count()).select_from(PublishJob).where(
            PublishJob.autopilot.is_(True),
            PublishJob.status.in_([PublishStatus.SCHEDULED, PublishStatus.QUEUED, PublishStatus.PUBLISHING]),
        )
    ) or 0


MAX_RETRIES_PER_DAY = 3   # failed videos per day after which a run slot is no longer freed


def failures_today(db: Session, row: AutopilotSettings, now: datetime) -> int:
    """How many picked videos failed since local midnight (each retry costs YouTube quota)."""
    midnight = now.astimezone(tz_of(row)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return db.scalar(
        select(func.count()).select_from(AutopilotSource).where(
            AutopilotSource.status == AutopilotSourceStatus.FAILED, AutopilotSource.picked_at >= midnight
        )
    ) or 0


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


SLOT_CLASH = timedelta(minutes=30)


def plan_slot_times(
    now: datetime,
    tz: ZoneInfo,
    count: int,
    existing: list[datetime],
    slots: list[time],
    posts_per_day: int,
) -> list[datetime]:
    """
    The next `count` free post times at the fixed daily `slots` (times of day in `tz`).

    A slot is skipped when it is already past (or less than 5 minutes away), when another post is within
    30 minutes of it, or when that calendar day already has `posts_per_day` posts (counting `existing`).
    """
    taken = list(existing)
    chosen: list[datetime] = []
    today = now.astimezone(tz).date()
    for offset in range(120):
        day = today + timedelta(days=offset)
        for slot in slots:
            if len(chosen) >= count:
                return chosen
            when = datetime.combine(day, slot, tzinfo=tz).astimezone(timezone.utc)
            if when < now + timedelta(minutes=5):
                continue
            if sum(1 for t in taken if t.astimezone(tz).date() == day) >= posts_per_day:
                break
            if any(abs(t - when) < SLOT_CLASH for t in taken):
                continue
            chosen.append(when)
            taken.append(when)
    return chosen


def _existing_post_times(db: Session, now: datetime) -> list[datetime]:
    """When non-failed posts are, or were, due: they count against the daily limit."""
    since = now - timedelta(days=3)
    rows = db.execute(
        select(PublishJob.published_at, PublishJob.scheduled_at, PublishJob.created_at)
        .where(
            PublishJob.status != PublishStatus.FAILED,
            or_(PublishJob.created_at >= since, PublishJob.scheduled_at >= now),
        )
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
        existing_times = _existing_post_times(db, now)
        slots = parse_post_times(row.post_times)
        if slots:  # the user chose fixed times of day
            times = plan_slot_times(now, tz, len(chosen), existing_times, slots, row.posts_per_day)
        else:
            times = plan_post_times(now, tz, len(chosen), existing_times, row.posts_per_day, first_hour)

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


# ─── Automatic retry of failed posts ─────────────────────────────────────────

RETRY_DELAYS = [timedelta(minutes=15), timedelta(hours=1), timedelta(hours=4), timedelta(hours=12)]
MAX_AUTO_RETRIES = 5            # manual and automatic tries together
MAX_POST_AGE = timedelta(hours=24)   # a post older than this is no longer worth posting late
STALE_CLOUD_AFTER = timedelta(hours=3)


def parse_failure(error_info: str | None) -> dict | None:
    """The stored failure details of a job, or None when there are none (or they are not readable)."""
    if not error_info:
        return None
    try:
        info = json.loads(error_info)
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def failure_is_retryable(info: dict) -> bool:
    """
    False for failures a retry cannot fix, or must never repeat:
    a sign-in that must be redone, a video that is already on YouTube (a retry would post it twice),
    a bad setting, or one we already gave up on.
    """
    return not (info.get("auth") or info.get("uploaded") or info.get("permanent") or info.get("gave_up"))


def _failed_at(info: dict) -> datetime | None:
    raw = info.get("failed_at")
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        return None


def retry_failed_jobs(db: Session, now: datetime, notify=alerts.send) -> int:
    """
    Put failed posts back so the next dispatch tries them again: after 15 minutes, 1 hour, 4 hours, then 12
    hours, at most MAX_AUTO_RETRIES times, and only while the post is under a day old. When a post cannot be
    retried any more the user is told once. Returns how many were put back.

    A post is put back with one conditional UPDATE (FAILED -> SCHEDULED at the same try count), so the laptop
    and the cloud job cannot both put back the same one.
    """
    requeued = 0
    for job in db.scalars(select(PublishJob).where(PublishJob.status == PublishStatus.FAILED)).all():
        info = parse_failure(job.error_info)
        failed_at = _failed_at(info) if info else None
        if info is None or failed_at is None or not failure_is_retryable(info):
            continue   # nothing recorded, or a failure that needs the user

        origin = _aware(job.scheduled_at or job.created_at)
        if now - origin > MAX_POST_AGE or job.retry_count >= MAX_AUTO_RETRIES:
            given_up = json.dumps({**info, "gave_up": True})
            changed = db.execute(
                update(PublishJob).where(PublishJob.id == job.id, PublishJob.error_info == job.error_info)
                .values(error_info=given_up)
            ).rowcount
            db.commit()
            if changed == 1:
                notify(
                    f"A post could not be sent after {job.retry_count} tries and was given up "
                    f"(last error: {str(info.get('error', ''))[:200]}). Press Retry on the Activity page if you still want it."
                )
            continue

        if now < failed_at + RETRY_DELAYS[min(job.retry_count, len(RETRY_DELAYS) - 1)]:
            continue

        job_id, tries = job.id, job.retry_count   # read now: the commit below expires the object
        claimed = db.execute(
            update(PublishJob)
            .where(PublishJob.id == job_id, PublishJob.status == PublishStatus.FAILED, PublishJob.retry_count == tries)
            .values(status=PublishStatus.SCHEDULED, retry_count=tries + 1, scheduled_at=job.scheduled_at or now)
        ).rowcount
        db.commit()
        if claimed == 1:
            requeued += 1
            logger.info(f"[AUTOPILOT] Retrying failed post {job_id} (try {tries + 1})")
    return requeued


def dispatch_lead(row: AutopilotSettings) -> timedelta:
    """
    How long before its time a post may be claimed and uploaded. With YouTube's own scheduling (public
    visibility, dry run off) the clip is uploaded early with a publish time, so a late scheduler cannot make
    it late. Otherwise a post goes out at its time.
    """
    if row.schedule_on_youtube and row.privacy == "public" and not row.dry_run:
        return EARLY_UPLOAD
    return timedelta(0)


# ─── Dispatcher for scheduled posts ──────────────────────────────────────────

def dispatch_due_publish_jobs(
    db: Session, now: datetime, publish, include_autopilot: bool = True, lead: timedelta = timedelta(0)
) -> int:
    """
    Queue every SCHEDULED publish job whose time has come. Returns how many were queued.

    Each job is claimed with one conditional UPDATE (SCHEDULED -> QUEUED), so two overlapping
    ticks cannot both queue the same post. With `include_autopilot=False` (the kill switch is off)
    only posts a person scheduled by hand go out. With a `lead`, posts due within that time are claimed too
    (they are uploaded early with YouTube's own publish time).
    """
    query = select(PublishJob.id).where(PublishJob.status == PublishStatus.SCHEDULED, PublishJob.scheduled_at <= now + lead)
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
    summary: dict = {"dispatched": 0, "retried": 0, "picked": None, "scheduled": [], "note": None}

    if row.auto_retry:
        summary["retried"] = retry_failed_jobs(db, now, notify)
    summary["dispatched"] = dispatch_due_publish_jobs(
        db, now, publish, include_autopilot=row.enabled, lead=dispatch_lead(row)
    )
    if row.last_cloud_run_at and now - _aware(row.last_cloud_run_at) > STALE_CLOUD_AFTER:
        notify(
            "The cloud publisher has not run for over 3 hours. GitHub may have paused it: open the Actions tab "
            "of the repository and re-enable the workflow.",
            key="cloud-stale", cooldown=12 * 3600,
        )
    failed = sync_source_statuses(db, now, notify)
    if failed and row.last_run_slot and failures_today(db, row, now) <= MAX_RETRIES_PER_DAY:
        row.last_run_slot = None  # this run slot produced nothing: let it pick the next candidate

    if row.enabled:
        summary["scheduled"] = finalize_ready_sources(db, row, now, notify)

        slot = latest_due_slot(row, now)
        if not force and (slot is None or slot == row.last_run_slot):
            summary["note"] = "no run slot due"
        elif in_flight(db):
            summary["note"] = "previous video still in progress"
        elif waiting_autopilot_posts(db) >= row.posts_per_day * 2:
            summary["note"] = "enough posts are already waiting"
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
