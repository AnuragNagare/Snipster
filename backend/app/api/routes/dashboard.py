"""The Dashboard: what Snipster has posted to YouTube, and the numbers each post got."""

import asyncio

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.core.logging import logger
from app.db.models import AutopilotSource, Clip, PublishJob, PublishStatus, SourceVideo
from app.db.schemas import DashboardPost, DashboardResponse, DashboardTotals
from app.services.youtube_stats import StatsUnavailable, fetch_video_stats

router = APIRouter(tags=["dashboard"])

DRY_RUN_POST_ID = "dry-run"


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(limit: int = Query(default=100, ge=1, le=200), db: AsyncSession = Depends(get_session)):
    """Every real post (rehearsals excluded), newest first, with views, likes and comments from YouTube."""
    real = (
        PublishJob.status == PublishStatus.PUBLISHED,
        PublishJob.platform_post_id.is_not(None),
        PublishJob.platform_post_id != "",
        PublishJob.platform_post_id != DRY_RUN_POST_ID,
    )
    rows = (
        await db.execute(
            select(PublishJob, Clip, SourceVideo)
            .join(Clip, PublishJob.clip_id == Clip.id)
            .join(SourceVideo, Clip.source_video_id == SourceVideo.id)
            .where(*real)
            .order_by(func.coalesce(PublishJob.published_at, PublishJob.created_at).desc())
            .limit(limit)
        )
    ).all()

    rehearsals = await db.scalar(
        select(func.count()).select_from(PublishJob).where(
            PublishJob.status == PublishStatus.PUBLISHED, PublishJob.platform_post_id == DRY_RUN_POST_ID
        )
    )

    titles = {}
    video_ids = [video.id for _, _, video in rows]
    if video_ids:
        picked = (
            await db.execute(select(AutopilotSource).where(AutopilotSource.source_video_id.in_(video_ids)))
        ).scalars().all()
        titles = {s.source_video_id: s.title for s in picked}

    stats: dict = {}
    stats_error = None
    post_ids = [job.platform_post_id for job, _, _ in rows]
    if post_ids:
        try:
            stats = await asyncio.to_thread(fetch_video_stats, post_ids)
        except StatsUnavailable as e:
            stats_error = str(e)
        except Exception as e:  # never let the numbers break the list of posts
            logger.error(f"[DASHBOARD] Unexpected error fetching stats: {e}")
            stats_error = "Could not load the numbers from YouTube."

    posts = []
    for job, clip, video in rows:
        found = stats.get(job.platform_post_id)
        posts.append(DashboardPost(
            id=job.id,
            video_id=job.platform_post_id,
            url=job.platform_post_url or f"https://www.youtube.com/shorts/{job.platform_post_id}",
            caption=clip.caption,
            source_title=titles.get(video.id) or video.original_filename or video.youtube_url,
            language=video.language,
            autopilot=bool(job.autopilot),
            published_at=job.published_at,
            has_stats=found is not None,
            views=found["views"] if found else None,
            likes=found["likes"] if found else None,
            comments=found["comments"] if found else None,
        ))

    counted = [p for p in posts if p.has_stats]
    totals = DashboardTotals(
        posts=len(posts),
        with_stats=len(counted),
        views=sum(p.views or 0 for p in counted),
        likes=sum(p.likes or 0 for p in counted),
        comments=sum(p.comments or 0 for p in counted),
    )
    return DashboardResponse(posts=posts, totals=totals, stats_error=stats_error, rehearsals=rehearsals or 0)
