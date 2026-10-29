"""Publish endpoints — trigger publishing and check job status."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_session
from app.db.models import ApprovalStatus, Clip, Platform, PublishJob, PublishStatus
from app.db.schemas import PublishJobResponse, PublishRequest
from app.workers.tasks import publish_clip_task
from app.core.logging import logger

router = APIRouter(prefix="/clips", tags=["publish"])


@router.post("/{clip_id}/publish", response_model=list[PublishJobResponse])
async def publish_clip(
    clip_id: uuid.UUID,
    payload: PublishRequest,
    db: AsyncSession = Depends(get_session),
):
    """Queue publishing of an approved clip to selected platforms."""
    clip = await db.get(Clip, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found.")

    if clip.approval_status != ApprovalStatus.APPROVED:
        raise HTTPException(
            status_code=400,
            detail="Clip must be approved before publishing.",
        )

    if not clip.storage_key:
        raise HTTPException(
            status_code=400,
            detail="Clip has not been rendered yet.",
        )

    jobs = []
    for platform in payload.platforms:
        # Check if a non-failed job already exists for this clip+platform
        existing = await db.execute(
            select(PublishJob).where(
                PublishJob.clip_id == clip_id,
                PublishJob.platform == platform,
                PublishJob.status.not_in([PublishStatus.FAILED]),
            )
        )
        if existing.scalars().first():
            logger.warning(f"Publish job already exists for clip {clip_id} / {platform}")
            continue

        job = PublishJob(
            clip_id=clip_id,
            platform=platform,
            status=PublishStatus.SCHEDULED if payload.scheduled_at else PublishStatus.QUEUED,
            scheduled_at=payload.scheduled_at,
        )
        db.add(job)
        await db.flush()
        await db.commit()  # the worker looks the job up by id — it must exist before it is queued

        if not payload.scheduled_at:
            publish_clip_task.delay(str(job.id))

        jobs.append(job)
        logger.info(f"Publish job created: {job.id} ({platform})")

    return [PublishJobResponse.model_validate(j) for j in jobs]


@router.get("/{clip_id}/publish-jobs", response_model=list[PublishJobResponse])
async def get_publish_jobs(
    clip_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
):
    """Get all publish jobs for a clip."""
    result = await db.execute(
        select(PublishJob).where(PublishJob.clip_id == clip_id)
    )
    return [PublishJobResponse.model_validate(j) for j in result.scalars().all()]


@router.post("/publish-jobs/{job_id}/retry", response_model=PublishJobResponse)
async def retry_publish_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
):
    """Manually retry a failed publish job."""
    job = await db.get(PublishJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Publish job not found.")

    if job.status != PublishStatus.FAILED:
        raise HTTPException(status_code=400, detail="Only failed jobs can be retried.")

    job.status = PublishStatus.QUEUED
    job.retry_count += 1
    await db.commit()
    publish_clip_task.delay(str(job.id))

    logger.info(f"Retry queued for publish job: {job_id}")
    return PublishJobResponse.model_validate(job)
