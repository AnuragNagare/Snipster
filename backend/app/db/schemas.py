"""Pydantic request/response schemas for the Snipster API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, HttpUrl

from app.db.models import (
    ApprovalStatus,
    Platform,
    PublishStatus,
    RenderStatus,
    VideoStatus,
)


# ─── Video ────────────────────────────────────────────────────────────────────

class VideoUploadResponse(BaseModel):
    id: uuid.UUID
    status: VideoStatus
    message: str


class UploadUrlRequest(BaseModel):
    filename: str
    content_type: str
    ownership_confirmed: bool


class UploadUrlResponse(BaseModel):
    upload_url: str       # PUT the file here
    storage_key: str      # send this back to /videos/upload-complete
    content_type: str     # the Content-Type header the PUT must carry (it is part of the signature)


class UploadCompleteRequest(BaseModel):
    storage_key: str
    filename: str
    ownership_confirmed: bool


class VideoFromURLRequest(BaseModel):
    youtube_url: str
    ownership_confirmed: bool


class VideoStatusResponse(BaseModel):
    id: uuid.UUID
    status: VideoStatus
    error_message: Optional[str] = None
    clip_count: int = 0

    class Config:
        from_attributes = True


# ─── Clip ─────────────────────────────────────────────────────────────────────

class ClipResponse(BaseModel):
    id: uuid.UUID
    source_video_id: uuid.UUID
    start_time: float
    end_time: float
    llm_reason: Optional[str] = None
    render_status: RenderStatus
    caption: Optional[str] = None
    hashtags: Optional[str] = None
    approval_status: ApprovalStatus
    preview_url: Optional[str] = None  # injected at response time, not stored
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ClipUpdateRequest(BaseModel):
    caption: Optional[str] = None
    hashtags: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None


class ClipRerenderRequest(BaseModel):
    start_time: float
    end_time: float


# ─── Publish ──────────────────────────────────────────────────────────────────

class PublishRequest(BaseModel):
    platforms: list[Platform]
    scheduled_at: Optional[datetime] = None  # None = publish immediately on approval


class PublishJobResponse(BaseModel):
    id: uuid.UUID
    clip_id: uuid.UUID
    platform: Platform
    status: PublishStatus
    platform_post_id: Optional[str] = None
    platform_post_url: Optional[str] = None
    error_info: Optional[str] = None
    retry_count: int
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ─── Auth ─────────────────────────────────────────────────────────────────────

class ConnectedAccountsResponse(BaseModel):
    youtube_connected: bool
    youtube_channel_name: Optional[str] = None
    instagram_connected: bool
    instagram_username: Optional[str] = None


# ─── Health ───────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
