"""Clip management endpoints — list, preview, edit, approve, reject, re-render."""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_session
from app.db.models import ApprovalStatus, Clip, SourceVideo
from app.db.schemas import ClipResponse, ClipRerenderRequest, ClipUpdateRequest
from app.services.storage import storage_service
from app.workers.tasks import render_clip_task
from app.core.logging import logger

router = APIRouter(prefix="/clips", tags=["clips"])


async def _get_clip_or_404(clip_id: uuid.UUID, db: AsyncSession) -> Clip:
    clip = await db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found.")
    return clip


def _inject_preview_url(clip: Clip) -> ClipResponse:
    """Build ClipResponse and inject a presigned preview URL."""
    response = ClipResponse.model_validate(clip)
    if clip.storage_key:
        try:
            response.preview_url = storage_service.get_presigned_url(clip.storage_key)
        except Exception:
            response.preview_url = None
    return response


@router.get("/video/{video_id}", response_model=list[ClipResponse])
async def list_clips_for_video(
    video_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
):
    """Return all clips generated from a source video."""
    video = await db.get(SourceVideo, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found.")

    result = await db.execute(select(Clip).where(Clip.source_video_id == video_id))
    clips = result.scalars().all()
    return [_inject_preview_url(c) for c in clips]


@router.get("/{clip_id}", response_model=ClipResponse)
async def get_clip(clip_id: uuid.UUID, db: AsyncSession = Depends(get_session)):
    """Get a single clip by ID."""
    clip = await _get_clip_or_404(clip_id, db)
    return _inject_preview_url(clip)


@router.get("/{clip_id}/preview-url")
async def get_preview_url(clip_id: uuid.UUID, db: AsyncSession = Depends(get_session)):
    """Return a fresh presigned URL for previewing the rendered clip."""
    clip = await _get_clip_or_404(clip_id, db)
    if not clip.storage_key:
        raise HTTPException(status_code=404, detail="Clip has not been rendered yet.")
    url = storage_service.get_presigned_url(clip.storage_key, expires_in=3600)
    return {"url": url}


@router.patch("/{clip_id}", response_model=ClipResponse)
async def update_clip(
    clip_id: uuid.UUID,
    payload: ClipUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    """Edit caption, hashtags, or trim points of a clip (without re-rendering)."""
    clip = await _get_clip_or_404(clip_id, db)

    if payload.caption is not None:
        clip.caption = payload.caption
    if payload.hashtags is not None:
        clip.hashtags = payload.hashtags
    if payload.start_time is not None:
        clip.start_time = payload.start_time
    if payload.end_time is not None:
        clip.end_time = payload.end_time

    return _inject_preview_url(clip)


@router.patch("/{clip_id}/approve", response_model=ClipResponse)
async def approve_clip(clip_id: uuid.UUID, db: AsyncSession = Depends(get_session)):
    """Mark a clip as approved (ready to publish)."""
    clip = await _get_clip_or_404(clip_id, db)
    clip.approval_status = ApprovalStatus.APPROVED
    logger.info(f"Clip approved: {clip_id}")
    return _inject_preview_url(clip)


@router.patch("/{clip_id}/reject", response_model=ClipResponse)
async def reject_clip(clip_id: uuid.UUID, db: AsyncSession = Depends(get_session)):
    """Mark a clip as rejected (will not be published)."""
    clip = await _get_clip_or_404(clip_id, db)
    clip.approval_status = ApprovalStatus.REJECTED
    logger.info(f"Clip rejected: {clip_id}")
    return _inject_preview_url(clip)


@router.post("/{clip_id}/rerender", response_model=ClipResponse)
async def rerender_clip(
    clip_id: uuid.UUID,
    payload: ClipRerenderRequest,
    db: AsyncSession = Depends(get_session),
):
    """Update trim points and queue a fresh render."""
    clip = await _get_clip_or_404(clip_id, db)

    duration = payload.end_time - payload.start_time
    if duration < 5 or duration > 90:
        raise HTTPException(
            status_code=400,
            detail=f"Clip duration must be 5–90 seconds (got {duration:.1f}s).",
        )

    clip.start_time = payload.start_time
    clip.end_time = payload.end_time
    clip.render_status = "pending"
    await db.commit()  # the worker reads the new trim points — they must be saved first

    # Queue a new render task
    render_clip_task.delay(str(clip_id))

    logger.info(f"Re-render queued for clip: {clip_id}")
    return _inject_preview_url(clip)
