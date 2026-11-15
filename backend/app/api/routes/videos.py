"""Video upload and submission endpoints."""

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_session
from app.db.models import SourceVideo, VideoStatus, Clip
from app.core.config import settings
from app.db.schemas import (
    LanguageResponse,
    LanguageUpdateRequest,
    UploadCompleteRequest,
    UploadUrlRequest,
    UploadUrlResponse,
    VideoFromURLRequest,
    VideoStatusResponse,
    VideoUploadResponse,
)
from app.services.storage import storage_service
from app.workers.pipeline import start_processing_pipeline
from app.core.logging import logger

router = APIRouter(prefix="/videos", tags=["videos"])

ALLOWED_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm"}
_OWNERSHIP_ERROR = "You must confirm you own the rights to this video before processing."
_SOURCE_KEY = re.compile(r"^source/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[^/]+$")


def _safe_filename(name: str) -> str:
    """Keep only the file name (no directories) and drop characters that are awkward in a storage key."""
    base = re.split(r"[\\/]", name)[-1].strip() or "video.mp4"
    return re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:200]


@router.post("/upload", response_model=VideoUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_video(
    file: Annotated[UploadFile, File(description="Video file to process")],
    ownership_confirmed: Annotated[bool, Form(description="User confirms they own the rights")],
    db: AsyncSession = Depends(get_session),
):
    """Upload a video file for processing into short clips."""
    if not ownership_confirmed:
        raise HTTPException(
            status_code=400,
            detail="You must confirm you own the rights to this video before processing.",
        )

    if file.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}")

    # Save to R2 storage
    storage_key = f"source/{uuid.uuid4()}/{file.filename}"
    try:
        file_bytes = await file.read()
        storage_service.upload_bytes(storage_key, file_bytes, content_type=file.content_type)
    except Exception as e:
        logger.error(f"Storage upload failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload video to storage.")

    # Create DB record
    video = SourceVideo(
        original_filename=file.filename,
        storage_key=storage_key,
        ownership_confirmed=True,
        status=VideoStatus.PENDING,
    )
    db.add(video)
    await db.flush()  # get the ID before commit
    # Commit BEFORE queueing: the worker is on another machine and may run at once,
    # and it must be able to find the row.
    await db.commit()

    # Kick off async processing pipeline
    start_processing_pipeline(str(video.id))

    logger.info(f"Video uploaded: {video.id} ({file.filename})")
    return VideoUploadResponse(
        id=video.id,
        status=video.status,
        message="Video uploaded. Processing started.",
    )


@router.post("/upload-url", response_model=UploadUrlResponse)
async def create_upload_url(payload: UploadUrlRequest):
    """
    Step 1 of a direct upload: get a short-lived URL to PUT the video to.

    The file goes browser -> storage, never through the API. That is required on
    Vercel, where a function request body is capped at 4.5 MB.
    """
    if not payload.ownership_confirmed:
        raise HTTPException(status_code=400, detail=_OWNERSHIP_ERROR)
    if payload.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {payload.content_type}")

    storage_key = f"source/{uuid.uuid4()}/{_safe_filename(payload.filename)}"
    try:
        upload_url = storage_service.get_presigned_upload_url(storage_key, payload.content_type)
    except Exception as e:
        logger.error(f"Could not sign upload URL: {e}")
        raise HTTPException(status_code=500, detail="Could not prepare the upload.")
    return UploadUrlResponse(upload_url=upload_url, storage_key=storage_key, content_type=payload.content_type)


@router.post("/upload-complete", response_model=VideoUploadResponse, status_code=status.HTTP_201_CREATED)
async def complete_upload(
    payload: UploadCompleteRequest,
    db: AsyncSession = Depends(get_session),
):
    """Step 2 of a direct upload: the file is in storage — create the video and start processing."""
    if not payload.ownership_confirmed:
        raise HTTPException(status_code=400, detail=_OWNERSHIP_ERROR)
    # Only keys that /upload-url could have produced — never an arbitrary object in the bucket.
    if not _SOURCE_KEY.match(payload.storage_key):
        raise HTTPException(status_code=400, detail="Invalid storage key.")

    existing = (
        await db.execute(select(SourceVideo).where(SourceVideo.storage_key == payload.storage_key))
    ).scalars().first()
    if existing:  # a retried request must not start a second pipeline
        return VideoUploadResponse(id=existing.id, status=existing.status, message="Already submitted.")

    try:
        size = storage_service.object_size(payload.storage_key)
    except Exception as e:
        logger.error(f"Storage check failed: {e}")
        raise HTTPException(status_code=500, detail="Could not verify the upload.")
    if not size:
        raise HTTPException(status_code=400, detail="The file was not found in storage. Upload it first.")
    if size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        storage_service.delete(payload.storage_key)
        raise HTTPException(status_code=400, detail=f"File is larger than {settings.MAX_UPLOAD_MB} MB.")

    video = SourceVideo(
        original_filename=_safe_filename(payload.filename),
        storage_key=payload.storage_key,
        ownership_confirmed=True,
        status=VideoStatus.PENDING,
    )
    db.add(video)
    await db.flush()
    await db.commit()  # before queueing — see upload_video

    start_processing_pipeline(str(video.id))
    logger.info(f"Video uploaded: {video.id} ({video.original_filename}, {size} bytes)")
    return VideoUploadResponse(
        id=video.id, status=video.status, message="Video uploaded. Processing started."
    )


@router.post("/from-url", response_model=VideoUploadResponse, status_code=status.HTTP_201_CREATED)
async def submit_video_url(
    payload: VideoFromURLRequest,
    db: AsyncSession = Depends(get_session),
):
    """Submit a YouTube URL for processing (downloads via yt-dlp)."""
    if not payload.ownership_confirmed:
        raise HTTPException(
            status_code=400,
            detail="You must confirm you own the rights to this video before processing.",
        )

    if "youtube.com" not in payload.youtube_url and "youtu.be" not in payload.youtube_url:
        raise HTTPException(status_code=400, detail="Only YouTube URLs are supported.")

    video = SourceVideo(
        youtube_url=payload.youtube_url,
        ownership_confirmed=True,
        status=VideoStatus.PENDING,
    )
    db.add(video)
    await db.flush()
    await db.commit()  # before queueing — see upload_video

    # Pipeline handles the download step first
    start_processing_pipeline(str(video.id), from_url=True)

    logger.info(f"Video URL submitted: {video.id} ({payload.youtube_url})")
    return VideoUploadResponse(
        id=video.id,
        status=video.status,
        message="URL submitted. Download and processing started.",
    )


@router.get("/{video_id}/status", response_model=VideoStatusResponse)
async def get_video_status(
    video_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
):
    """Poll the processing status of a submitted video."""
    video = await db.get(SourceVideo, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found.")

    # Count how many clips have been created
    result = await db.execute(
        select(Clip).where(Clip.source_video_id == video_id)
    )
    clips = result.scalars().all()

    return VideoStatusResponse(
        id=video.id,
        status=video.status,
        error_message=video.error_message,
        clip_count=len(clips),
        language=video.language,
    )


@router.patch("/{video_id}/language", response_model=LanguageResponse)
async def set_video_language(
    video_id: uuid.UUID,
    payload: LanguageUpdateRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Correct the language Whisper detected (it can be wrong, e.g. Hinglish).

    It applies to captions generated and clips rendered from now on; existing captions and
    rendered files are not touched. Regenerate or re-render a clip to pick it up.
    """
    video = await db.get(SourceVideo, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found.")
    video.language = payload.language
    logger.info(f"Language set to {payload.language} for video {video_id}")
    return LanguageResponse(id=video.id, language=video.language)
