"""Autopilot settings, the Activity feed, and the manual "run now"."""

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.core.config import settings
from app.core.logging import logger
from app.db.models import (
    AutopilotSettings,
    AutopilotSource,
    Clip,
    PublishJob,
    PublishStatus,
    SourceVideo,
    User,
)
from app.db.schemas import (
    ActivityJob,
    ActivityResponse,
    ActivitySource,
    AutopilotSettingsResponse,
    AutopilotSettingsUpdate,
    RunNowResponse,
)
from app.services.autopilot import parse_post_times, parse_run_hours
from app.workers.tasks import autopilot_tick_task

router = APIRouter(prefix="/autopilot", tags=["autopilot"])
activity_router = APIRouter(tags=["activity"])

DRY_RUN_POST_ID = "dry-run"


async def _get_row(db: AsyncSession) -> AutopilotSettings:
    """The single settings row, created with the safe defaults (off, dry run, private) on first use."""
    row = await db.get(AutopilotSettings, 1)
    if row is None:
        row = AutopilotSettings(
            id=1, enabled=False, dry_run=True, auto_approve=False, posts_per_day=3,
            clips_per_video=2, run_times="9,18", timezone="UTC", privacy="private",
            source_mode="own", source_language="en", post_times="",
        )
        db.add(row)
        await db.flush()
    return row


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _settings_response(db: AsyncSession, row: AutopilotSettings) -> AutopilotSettingsResponse:
    now = datetime.now(timezone.utc)
    try:
        tz = ZoneInfo(row.timezone or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    day_start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    posted_today = await db.scalar(
        select(func.count()).select_from(PublishJob).where(
            PublishJob.status == PublishStatus.PUBLISHED, PublishJob.published_at >= day_start
        )
    )
    scheduled = await db.scalar(
        select(func.count()).select_from(PublishJob).where(
            PublishJob.status.in_([PublishStatus.SCHEDULED, PublishStatus.QUEUED, PublishStatus.PUBLISHING])
        )
    )
    user = (await db.execute(select(User).limit(1))).scalars().first()
    return AutopilotSettingsResponse(
        enabled=row.enabled, dry_run=row.dry_run, auto_approve=row.auto_approve,
        posts_per_day=row.posts_per_day, clips_per_video=row.clips_per_video,
        run_times=row.run_times, timezone=row.timezone, privacy=row.privacy,
        source_mode=row.source_mode or "own", source_language=row.source_language or "en",
        post_times=row.post_times or "",
        last_tick_at=row.last_tick_at,
        seconds_since_tick=int((now - _aware(row.last_tick_at)).total_seconds()) if row.last_tick_at else None,
        tick_minutes=settings.AUTOPILOT_TICK_MINUTES,
        posted_today=posted_today or 0,
        scheduled_count=scheduled or 0,
        youtube_connected=bool(user and user.yt_access_token),
    )


@router.get("/settings", response_model=AutopilotSettingsResponse)
async def get_autopilot_settings(db: AsyncSession = Depends(get_session)):
    return await _settings_response(db, await _get_row(db))


@router.patch("/settings", response_model=AutopilotSettingsResponse)
async def update_autopilot_settings(
    payload: AutopilotSettingsUpdate, db: AsyncSession = Depends(get_session)
):
    """Change any of the settings. `enabled=false` is the kill switch."""
    row = await _get_row(db)
    changes = payload.model_dump(exclude_none=True)

    if "run_times" in changes:
        hours = parse_run_hours(changes["run_times"])
        given = [p.strip() for p in changes["run_times"].split(",") if p.strip()]
        if not hours or len(hours) != len(set(given)):
            raise HTTPException(status_code=422, detail="Run times must be hours from 0 to 23, like 9,18.")
        changes["run_times"] = ",".join(str(h) for h in hours)
    if "post_times" in changes:
        text = changes["post_times"].strip()
        given = [p.strip() for p in text.split(",") if p.strip()]
        parsed = parse_post_times(text)
        if text and (not parsed or len(parsed) != len(set(given))):
            raise HTTPException(status_code=422, detail="Post times must look like 19:45 or 12:15,19:45 (24-hour clock).")
        changes["post_times"] = ",".join(f"{t:%H:%M}" for t in parsed)
    if "timezone" in changes:
        try:
            ZoneInfo(changes["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(status_code=422, detail=f"Unknown timezone: {changes['timezone']}")

    for key, value in changes.items():
        setattr(row, key, value)
    if changes:
        logger.info(f"[AUTOPILOT] Settings changed: {sorted(changes)}")
    await db.flush()
    return await _settings_response(db, row)


@router.post("/run-now", response_model=RunNowResponse)
async def run_now(db: AsyncSession = Depends(get_session)):
    """Ask the worker to pick the next video now instead of waiting for the next run time."""
    row = await _get_row(db)
    if not row.enabled:
        raise HTTPException(status_code=400, detail="Turn autopilot on first.")
    await db.commit()  # the worker reads the settings row
    try:
        autopilot_tick_task.delay(force=True)
    except Exception as e:
        logger.error(f"Could not queue run-now: {e}")
        raise HTTPException(status_code=503, detail="Could not reach the job queue.")
    return RunNowResponse(message="Queued. It starts when the worker is free.")


@activity_router.get("/activity", response_model=ActivityResponse)
async def get_activity(limit: int = Query(default=50, ge=1, le=200), db: AsyncSession = Depends(get_session)):
    """Everything posted, scheduled or failed, newest first, plus the videos autopilot has picked."""
    when = func.coalesce(PublishJob.published_at, PublishJob.scheduled_at, PublishJob.created_at)
    rows = (
        await db.execute(
            select(PublishJob, Clip, SourceVideo)
            .join(Clip, PublishJob.clip_id == Clip.id)
            .join(SourceVideo, Clip.source_video_id == SourceVideo.id)
            .order_by(when.desc())
            .limit(limit)
        )
    ).all()

    video_ids = [video.id for _, _, video in rows]
    titles = {}
    if video_ids:
        picked = (
            await db.execute(select(AutopilotSource).where(AutopilotSource.source_video_id.in_(video_ids)))
        ).scalars().all()
        titles = {s.source_video_id: s.title for s in picked}

    jobs = []
    for job, clip, video in rows:
        error, needs_reconnect = None, False
        if job.error_info:
            try:
                info = json.loads(job.error_info)
                error, needs_reconnect = info.get("error"), bool(info.get("auth"))
            except ValueError:
                error = job.error_info
        jobs.append(ActivityJob(
            id=job.id, clip_id=clip.id, status=job.status, caption=clip.caption,
            source_title=titles.get(video.id) or video.original_filename or video.youtube_url,
            language=video.language, autopilot=bool(job.autopilot),
            dry_run=job.platform_post_id == DRY_RUN_POST_ID,
            post_url=job.platform_post_url or None, error=error, needs_reconnect=needs_reconnect,
            scheduled_at=job.scheduled_at, published_at=job.published_at, created_at=job.created_at,
        ))

    sources = (
        await db.execute(select(AutopilotSource).order_by(AutopilotSource.picked_at.desc()).limit(20))
    ).scalars().all()
    return ActivityResponse(
        jobs=jobs,
        sources=[ActivitySource.model_validate(s, from_attributes=True) for s in sources],
    )
