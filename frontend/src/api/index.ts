import axios from "axios";
import api from "./client";
import type {
  ActivityResponse,
  AutopilotSettings,
  AutopilotSettingsUpdate,
  Clip,
  ClipUpdateRequest,
  ConnectedAccounts,
  Platform,
  PublishJob,
  SubtitleLine,
  SubtitlesResponse,
  UploadUrlResponse,
  VideoStatusResponse,
  VideoUploadResponse,
} from "../types";

// ─── Videos ──────────────────────────────────────────────────────────────────

/**
 * Upload goes straight to storage, not through the API: a Vercel Function
 * rejects request bodies over 4.5 MB. Three steps — get a signed URL, PUT the
 * file to it, then tell the API it arrived.
 */
export async function uploadVideo(
  file: File,
  ownershipConfirmed: boolean,
  onProgress?: (percent: number) => void
): Promise<VideoUploadResponse> {
  const { data: slot } = await api.post<UploadUrlResponse>("/videos/upload-url", {
    filename: file.name,
    content_type: file.type || "video/mp4",
    ownership_confirmed: ownershipConfirmed,
  });

  try {
    // A bare axios call, not `api`: the login token must not be sent to storage.
    await axios.put(slot.upload_url, file, {
      headers: { "Content-Type": slot.content_type },
      onUploadProgress: (e) => {
        if (e.total) onProgress?.(Math.round((e.loaded / e.total) * 100));
      },
    });
  } catch (err: any) {
    // Same shape as an API error, so the page shows it like any other.
    const detail = err?.response
      ? `Storage rejected the upload (${err.response.status}).`
      : "Could not reach storage. The bucket's CORS settings must allow uploads from this site.";
    throw Object.assign(new Error(detail), { response: { data: { detail } } });
  }

  const res = await api.post<VideoUploadResponse>("/videos/upload-complete", {
    storage_key: slot.storage_key,
    filename: file.name,
    ownership_confirmed: ownershipConfirmed,
  });
  return res.data;
}

export async function submitVideoUrl(
  url: string,
  ownershipConfirmed: boolean
): Promise<VideoUploadResponse> {
  const res = await api.post<VideoUploadResponse>("/videos/from-url", {
    youtube_url: url,
    ownership_confirmed: ownershipConfirmed,
  });
  return res.data;
}

export async function getVideoStatus(
  videoId: string
): Promise<VideoStatusResponse> {
  const res = await api.get<VideoStatusResponse>(`/videos/${videoId}/status`);
  return res.data;
}

/** Correct the language Whisper detected. Affects captions and renders made afterwards. */
export async function setVideoLanguage(videoId: string, language: "en" | "hi"): Promise<string> {
  const res = await api.patch<{ id: string; language: string }>(`/videos/${videoId}/language`, {
    language,
  });
  return res.data.language;
}

// ─── Clips ───────────────────────────────────────────────────────────────────

export async function getClipsForVideo(videoId: string): Promise<Clip[]> {
  const res = await api.get<Clip[]>(`/clips/video/${videoId}`);
  return res.data;
}

export async function getClip(clipId: string): Promise<Clip> {
  const res = await api.get<Clip>(`/clips/${clipId}`);
  return res.data;
}

/** Queues a fresh caption + hashtags in the video's current language. The new text arrives later. */
export async function regenerateCaption(clipId: string): Promise<void> {
  await api.post(`/clips/${clipId}/regenerate-caption`);
}

export async function updateClip(
  clipId: string,
  data: ClipUpdateRequest
): Promise<Clip> {
  const res = await api.patch<Clip>(`/clips/${clipId}`, data);
  return res.data;
}

export async function approveClip(clipId: string): Promise<Clip> {
  const res = await api.patch<Clip>(`/clips/${clipId}/approve`);
  return res.data;
}

export async function rejectClip(clipId: string): Promise<Clip> {
  const res = await api.patch<Clip>(`/clips/${clipId}/reject`);
  return res.data;
}

export async function rerenderClip(
  clipId: string,
  startTime: number,
  endTime: number
): Promise<Clip> {
  const res = await api.post<Clip>(`/clips/${clipId}/rerender`, {
    start_time: startTime,
    end_time: endTime,
  });
  return res.data;
}

export async function getPreviewUrl(
  clipId: string
): Promise<string> {
  const res = await api.get<{ url: string }>(`/clips/${clipId}/preview-url`);
  return res.data.url;
}

export async function getSubtitles(clipId: string): Promise<SubtitlesResponse> {
  const res = await api.get<SubtitlesResponse>(`/clips/${clipId}/subtitles`);
  return res.data;
}

/** Saves the lines. They are burned in at the next re-render, not before. */
export async function updateSubtitles(
  clipId: string,
  lines: SubtitleLine[]
): Promise<SubtitlesResponse> {
  const res = await api.patch<SubtitlesResponse>(`/clips/${clipId}/subtitles`, { lines });
  return res.data;
}

// ─── Publish ──────────────────────────────────────────────────────────────────

export async function publishClip(
  clipId: string,
  platforms: Platform[],
  scheduledAt?: string
): Promise<PublishJob[]> {
  const res = await api.post<PublishJob[]>(`/clips/${clipId}/publish`, {
    platforms,
    scheduled_at: scheduledAt ?? null,
  });
  return res.data;
}

export async function getPublishJobs(clipId: string): Promise<PublishJob[]> {
  const res = await api.get<PublishJob[]>(`/clips/${clipId}/publish-jobs`);
  return res.data;
}

export async function retryPublishJob(jobId: string): Promise<PublishJob> {
  const res = await api.post<PublishJob>(`/clips/publish-jobs/${jobId}/retry`);
  return res.data;
}

// ─── Auth ─────────────────────────────────────────────────────────────────────

export async function getConnectedAccounts(): Promise<ConnectedAccounts> {
  const res = await api.get<ConnectedAccounts>("/auth/accounts");
  return res.data;
}

/** Where to send the browser to connect an account. The server signs the request, so this needs the login. */
export async function startGoogleLogin(): Promise<string> {
  const res = await api.post<{ url: string }>("/auth/google/start");
  return res.data.url;
}

export async function disconnectYoutube(): Promise<void> {
  await api.delete("/auth/google/disconnect");
}

// ─── Login ────────────────────────────────────────────────────────────────────

export async function getAuthConfig(): Promise<{ auth_required: boolean; misconfigured: boolean }> {
  const res = await api.get<{ auth_required: boolean; misconfigured: boolean }>("/auth/config");
  return res.data;
}

export async function login(password: string): Promise<string> {
  const res = await api.post<{ token: string }>("/auth/session", { password });
  return res.data.token;
}

export async function checkSession(): Promise<void> {
  await api.get("/auth/session");
}

// ─── System ───────────────────────────────────────────────────────────────────

export interface SystemInfo {
  whisper_device: "cuda" | "cpu";
  whisper_compute_type: string;
  whisper_model: string;
  ffmpeg_encoder: "h264_nvenc" | "libx264";
  using_gpu: boolean;
}

export async function getSystemInfo(): Promise<SystemInfo> {
  const res = await api.get<SystemInfo>("/system-info");
  return res.data;
}


// ─── Autopilot ────────────────────────────────────────────────────────────────

export async function getAutopilotSettings(): Promise<AutopilotSettings> {
  const res = await api.get<AutopilotSettings>("/autopilot/settings");
  return res.data;
}

export async function updateAutopilotSettings(
  changes: AutopilotSettingsUpdate
): Promise<AutopilotSettings> {
  const res = await api.patch<AutopilotSettings>("/autopilot/settings", changes);
  return res.data;
}

export async function runAutopilotNow(): Promise<string> {
  const res = await api.post<{ message: string }>("/autopilot/run-now");
  return res.data.message;
}

export async function getActivity(limit = 50): Promise<ActivityResponse> {
  const res = await api.get<ActivityResponse>("/activity", { params: { limit } });
  return res.data;
}
