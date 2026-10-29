// Shared TypeScript types matching backend Pydantic schemas

export type VideoStatus =
  | "pending"
  | "transcribing"
  | "selecting_clips"
  | "ready"
  | "failed";

export type RenderStatus = "pending" | "rendering" | "done" | "failed";

export type ApprovalStatus = "pending" | "approved" | "rejected";

export type Platform = "youtube" | "instagram";

export type PublishStatus =
  | "queued"
  | "publishing"
  | "published"
  | "failed"
  | "scheduled";

export interface VideoUploadResponse {
  id: string;
  status: VideoStatus;
  message: string;
}

export interface UploadUrlResponse {
  upload_url: string;
  storage_key: string;
  content_type: string;
}

export interface VideoStatusResponse {
  id: string;
  status: VideoStatus;
  error_message?: string;
  clip_count: number;
}

export interface Clip {
  id: string;
  source_video_id: string;
  start_time: number;
  end_time: number;
  llm_reason?: string;
  render_status: RenderStatus;
  caption?: string;
  hashtags?: string;
  approval_status: ApprovalStatus;
  preview_url?: string;
  created_at: string;
  updated_at: string;
}

export interface ClipUpdateRequest {
  caption?: string;
  hashtags?: string;
  start_time?: number;
  end_time?: number;
}

export interface PublishJob {
  id: string;
  clip_id: string;
  platform: Platform;
  status: PublishStatus;
  platform_post_id?: string;
  platform_post_url?: string;
  error_info?: string;
  retry_count: number;
  scheduled_at?: string;
  published_at?: string;
}

export interface ConnectedAccounts {
  youtube_connected: boolean;
  youtube_channel_name?: string;
  instagram_connected: boolean;
  instagram_username?: string;
}
