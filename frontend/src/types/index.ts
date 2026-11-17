// Shared TypeScript types matching backend Pydantic schemas

export type VideoStatus =
  | "pending"
  | "transcribing"
  | "selecting_clips"
  | "ready"
  | "failed";

export type RenderStatus = "pending" | "rendering" | "done" | "failed";

export type ApprovalStatus = "pending" | "approved" | "rejected";

export type Platform = "youtube";

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
  language?: string;
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
  language?: string; // ISO code of the source video's spoken language, e.g. "en", "hi"
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

/** One burned-in subtitle line. Times are seconds from the start of the clip. */
export interface SubtitleLine {
  start: number;
  end: number;
  text: string;
}

export interface SubtitlesResponse {
  lines: SubtitleLine[];
  /** false = built from the transcript, true = saved by the user */
  customized: boolean;
}

export interface ConnectedAccounts {
  youtube_connected: boolean;
  youtube_channel_name?: string;
}

// ─── Autopilot / Activity ─────────────────────────────────────────────────────

export interface AutopilotSettings {
  enabled: boolean;
  dry_run: boolean;
  auto_approve: boolean;
  posts_per_day: number;
  clips_per_video: number;
  run_times: string;
  timezone: string;
  privacy: "private" | "unlisted" | "public";
  /** where videos come from: your own channel, or Creative Commons videos that are credited */
  source_mode: "own" | "creative_commons";
  source_language: "en" | "hi";
  /** fixed times of day to post at, in `timezone`, like "12:15,19:45"; empty = spread evenly */
  post_times: string;
  last_tick_at?: string;
  /** null = the scheduler has never run */
  seconds_since_tick: number | null;
  tick_minutes: number;
  posted_today: number;
  scheduled_count: number;
  youtube_connected: boolean;
}

export type AutopilotSettingsUpdate = Partial<
  Pick<
    AutopilotSettings,
    | "enabled"
    | "dry_run"
    | "auto_approve"
    | "posts_per_day"
    | "clips_per_video"
    | "run_times"
    | "timezone"
    | "privacy"
    | "source_mode"
    | "source_language"
    | "post_times"
  >
>;

export interface ActivityJob {
  id: string;
  clip_id: string;
  status: PublishStatus;
  caption?: string;
  source_title?: string;
  language?: string;
  autopilot: boolean;
  /** a rehearsal: nothing was uploaded */
  dry_run: boolean;
  post_url?: string;
  error?: string;
  /** the YouTube sign-in must be redone before a retry can work */
  needs_reconnect: boolean;
  scheduled_at?: string;
  published_at?: string;
  created_at?: string;
}

export interface ActivitySource {
  id: string;
  youtube_video_id: string;
  title?: string;
  status: "picked" | "processing" | "done" | "skipped" | "failed";
  reason?: string;
  picked_at?: string;
  source_video_id?: string;
  /** the creator, for Creative Commons picks */
  channel_title?: string;
  license?: string;
  views?: number;
  likes?: number;
}

export interface ActivityResponse {
  jobs: ActivityJob[];
  sources: ActivitySource[];
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

export interface DashboardPost {
  id: string;
  video_id: string;
  url: string;
  caption?: string;
  source_title?: string;
  language?: string;
  autopilot: boolean;
  published_at?: string;
  /** false: YouTube returned nothing for it (private or deleted video, or no key) */
  has_stats: boolean;
  views?: number;
  /** also missing when the owner hides the like count */
  likes?: number;
  comments?: number;
}

export interface DashboardTotals {
  posts: number;
  /** posts whose numbers are included in the sums */
  with_stats: number;
  views: number;
  likes: number;
  comments: number;
}

export interface DashboardResponse {
  posts: DashboardPost[];
  totals: DashboardTotals;
  /** why the numbers are missing; the posts are still listed */
  stats_error?: string;
  /** dry-run posts left out of the list */
  rehearsals: number;
}
