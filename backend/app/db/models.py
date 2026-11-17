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
    language: Mapped[str] = mapped_column(String(8), nullable=True)  # ISO code Whisper detected, e.g. "en", "hi"
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

    # User-edited burned-in subtitles: JSON list of {"start", "end", "text"}, times relative to the
    # clip start. NULL = not edited, build them from the transcript. [] = user removed all subtitles.
    subtitles: Mapped[str] = mapped_column(Text, nullable=True)

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
    # Created by autopilot (not by a person clicking Publish). Only these jobs obey the kill switch
    # and dry run, so a manual publish is never silently skipped.
    autopilot: Mapped[bool] = mapped_column(Boolean, default=False)

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    clip: Mapped["Clip"] = relationship(back_populates="publish_jobs")


# ─── Autopilot ────────────────────────────────────────────────────────────────

class AutopilotSource(Base):
    """A video from the user's own channel that autopilot has picked. One row per YouTube video, ever."""
    __tablename__ = "autopilot_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    youtube_video_id: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(512), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=True)
    # picked -> processing -> done, or skipped / failed. Plain strings (see AutopilotSourceStatus).
    status: Mapped[str] = mapped_column(String(16), default="picked")
    source_video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_videos.id"), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=True)  # why skipped / failed
    # Who made the source video and under which licence. Filled for Creative Commons picks, where the
    # creator must be credited in every post's description. NULL for the user's own videos.
    channel_id: Mapped[str] = mapped_column(String(64), nullable=True)
    channel_title: Mapped[str] = mapped_column(String(256), nullable=True)
    license: Mapped[str] = mapped_column(String(32), nullable=True)   # "creativeCommon"
    views: Mapped[int] = mapped_column(Integer, nullable=True)        # at the time it was picked
    likes: Mapped[int] = mapped_column(Integer, nullable=True)
    picked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AutopilotSourceStatus:
    PICKED = "picked"
    PROCESSING = "processing"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    IN_FLIGHT = ("picked", "processing")


class AutopilotSettings(Base):
    """The one settings row for autopilot. Always id = 1. Safe defaults: off, dry run, private."""
    __tablename__ = "autopilot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    posts_per_day: Mapped[int] = mapped_column(Integer, default=3)
    clips_per_video: Mapped[int] = mapped_column(Integer, default=2)
    # Hours of the day (0-23) at which autopilot picks a new video, comma-separated, in `timezone`.
    run_times: Mapped[str] = mapped_column(String(64), default="9,18")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    # Applies to EVERY upload the app makes, manual or automatic, once this row exists.
    privacy: Mapped[str] = mapped_column(String(16), default="private")
    # Where videos come from: "own" = the user's channel, "creative_commons" = trending videos whose
    # owners licensed them for reuse (credited in every post). Never any other creator's videos.
    source_mode: Mapped[str] = mapped_column(String(24), default="own")
    source_language: Mapped[str] = mapped_column(String(8), default="en")  # "en" or "hi", for Creative Commons search
    # Fixed times of day to post at, in `timezone`: "19:45,12:15". Empty = spread posts evenly.
    post_times: Mapped[str] = mapped_column(String(64), default="")

    last_tick_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_slot: Mapped[str] = mapped_column(String(32), nullable=True)  # e.g. "2026-11-15T09"
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
