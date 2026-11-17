"""Pydantic request/response schemas for the Snipster API."""

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl

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


class LanguageUpdateRequest(BaseModel):
    language: Literal["en", "hi"]


class LanguageResponse(BaseModel):
    id: uuid.UUID
    language: str


class VideoStatusResponse(BaseModel):
    id: uuid.UUID
    status: VideoStatus
    error_message: Optional[str] = None
    clip_count: int = 0
    language: Optional[str] = None

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
    language: Optional[str] = None  # copied from the source video at response time, not stored on the clip
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


class SubtitleLine(BaseModel):
    """One burned-in subtitle line. Times are seconds from the start of the clip."""
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=200)


class SubtitlesUpdateRequest(BaseModel):
    lines: list[SubtitleLine] = Field(max_length=300)


class SubtitlesResponse(BaseModel):
    lines: list[SubtitleLine]
    customized: bool  # False = built from the transcript, True = saved by the user


class RegenerateCaptionResponse(BaseModel):
    clip_id: uuid.UUID
    message: str


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


# ─── Health ───────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"


# ─── Autopilot / Activity ─────────────────────────────────────────────────────

class AutopilotSettingsResponse(BaseModel):
    enabled: bool
    dry_run: bool
    auto_approve: bool
    posts_per_day: int
    clips_per_video: int
    run_times: str
    timezone: str
    privacy: str
    last_tick_at: Optional[datetime] = None
    seconds_since_tick: Optional[int] = None  # None = the scheduler has never run
    tick_minutes: int
    posted_today: int
    scheduled_count: int
    youtube_connected: bool


class AutopilotSettingsUpdate(BaseModel):
    """Every field is optional: only the ones sent are changed."""
    enabled: Optional[bool] = None
    dry_run: Optional[bool] = None
    auto_approve: Optional[bool] = None
    posts_per_day: Optional[int] = Field(default=None, ge=1, le=10)
    clips_per_video: Optional[int] = Field(default=None, ge=1, le=5)
    run_times: Optional[str] = Field(default=None, max_length=64)
    timezone: Optional[str] = Field(default=None, max_length=64)
    privacy: Optional[Literal["private", "unlisted", "public"]] = None


class RunNowResponse(BaseModel):
    message: str


class ActivityJob(BaseModel):
    id: uuid.UUID
    clip_id: uuid.UUID
    status: PublishStatus
    caption: Optional[str] = None
    source_title: Optional[str] = None
    language: Optional[str] = None
    autopilot: bool
    dry_run: bool = False          # a rehearsal: nothing was uploaded
    post_url: Optional[str] = None
    error: Optional[str] = None
    needs_reconnect: bool = False  # the YouTube sign-in must be redone before a retry can work
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class ActivitySource(BaseModel):
    id: uuid.UUID
    youtube_video_id: str
    title: Optional[str] = None
    status: str
    reason: Optional[str] = None
    picked_at: Optional[datetime] = None
    source_video_id: Optional[uuid.UUID] = None


class ActivityResponse(BaseModel):
    jobs: list[ActivityJob]
    sources: list[ActivitySource]


# ─── Dashboard ────────────────────────────────────────────────────────────────

class DashboardPost(BaseModel):
    id: uuid.UUID
    video_id: str                  # the YouTube video id
    url: str
    caption: Optional[str] = None
    source_title: Optional[str] = None
    language: Optional[str] = None
    autopilot: bool
    published_at: Optional[datetime] = None
    has_stats: bool                # False: YouTube returned nothing (private or deleted video, or no key)
    views: Optional[int] = None
    likes: Optional[int] = None    # None also when the owner hides the like count
    comments: Optional[int] = None


class DashboardTotals(BaseModel):
    posts: int
    with_stats: int                # posts whose numbers are included in the sums below
    views: int
    likes: int
    comments: int


class DashboardResponse(BaseModel):
    posts: list[DashboardPost]
    totals: DashboardTotals
    stats_error: Optional[str] = None  # why the numbers are missing; the posts are still listed
    rehearsals: int = 0                # dry-run posts left out of the list
