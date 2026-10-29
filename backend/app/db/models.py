"""SQLAlchemy ORM models mapping to the Snipster database schema."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


# ─── Enums ────────────────────────────────────────────────────────────────────

class VideoStatus(str, enum.Enum):
    PENDING = "pending"
    TRANSCRIBING = "transcribing"
    SELECTING_CLIPS = "selecting_clips"
    READY = "ready"
    FAILED = "failed"


class RenderStatus(str, enum.Enum):
    PENDING = "pending"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Platform(str, enum.Enum):
    YOUTUBE = "youtube"


class PublishStatus(str, enum.Enum):
    QUEUED = "queued"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    SCHEDULED = "scheduled"


# ─── Models ───────────────────────────────────────────────────────────────────

class User(Base):
    """Single-user model storing the OAuth tokens for YouTube."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    display_name: Mapped[str] = mapped_column(String(256), nullable=True)
    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=True)

    # YouTube OAuth (encrypted)
    yt_access_token: Mapped[str] = mapped_column(Text, nullable=True)
    yt_refresh_token: Mapped[str] = mapped_column(Text, nullable=True)
    yt_token_expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    yt_channel_id: Mapped[str] = mapped_column(String(128), nullable=True)
    yt_channel_name: Mapped[str] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    videos: Mapped[list["SourceVideo"]] = relationship(back_populates="user")


class SourceVideo(Base):
    """A long-form video submitted by the user for processing."""
    __tablename__ = "source_videos"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=True)

    # Source
    original_filename: Mapped[str] = mapped_column(String(512), nullable=True)
    youtube_url: Mapped[str] = mapped_column(String(1024), nullable=True)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=True)  # R2 object key

    # Rights confirmation (required before processing)
    ownership_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Processing
    status: Mapped[VideoStatus] = mapped_column(
        Enum(VideoStatus), default=VideoStatus.PENDING
    )
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=True)
    transcript: Mapped[str] = mapped_column(Text, nullable=True)  # JSON string
    error_message: Mapped[str] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="videos")
    clips: Mapped[list["Clip"]] = relationship(back_populates="source_video")


class Clip(Base):
    """A short clip derived from a SourceVideo."""
    __tablename__ = "clips"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_videos.id"))

    # Timestamps from LLM selection
    start_time: Mapped[float] = mapped_column(Float)   # seconds
    end_time: Mapped[float] = mapped_column(Float)     # seconds
    llm_reason: Mapped[str] = mapped_column(Text, nullable=True)  # why the LLM picked this

    # Rendered output
    render_status: Mapped[RenderStatus] = mapped_column(
        Enum(RenderStatus), default=RenderStatus.PENDING
    )
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=True)  # R2 key for rendered .mp4

    # AI-generated content
    caption: Mapped[str] = mapped_column(Text, nullable=True)
    hashtags: Mapped[str] = mapped_column(Text, nullable=True)

    # User decision
    approval_status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus), default=ApprovalStatus.PENDING
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    source_video: Mapped["SourceVideo"] = relationship(back_populates="clips")
    publish_jobs: Mapped[list["PublishJob"]] = relationship(back_populates="clip")


class PublishJob(Base):
    """Tracks a publish attempt for one clip to one platform."""
    __tablename__ = "publish_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    clip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clips.id"))
    platform: Mapped[Platform] = mapped_column(Enum(Platform))

    status: Mapped[PublishStatus] = mapped_column(
        Enum(PublishStatus), default=PublishStatus.QUEUED
    )
    platform_post_id: Mapped[str] = mapped_column(String(512), nullable=True)
    platform_post_url: Mapped[str] = mapped_column(String(1024), nullable=True)
    error_info: Mapped[str] = mapped_column(Text, nullable=True)  # JSON string
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    clip: Mapped["Clip"] = relationship(back_populates="publish_jobs")
