import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Loader2, CheckCircle, XCircle, Cpu, Zap } from "lucide-react";
import { getVideoStatus, getSystemInfo } from "../api";
import type { SystemInfo } from "../api";
import type { VideoStatus } from "../types";

const STEPS: { status: VideoStatus; label: string }[] = [
  { status: "transcribing", label: "Transcribing audio with Whisper…" },
  { status: "selecting_clips", label: "AI selecting best clip moments…" },
  { status: "ready", label: "Rendering clips & generating captions…" },
];

function getStepIndex(status: VideoStatus): number {
  return STEPS.findIndex((s) => s.status === status);
}

// ─── GPU / CPU Badge ──────────────────────────────────────────────────────────

function ComputeBadge({ info }: { info: SystemInfo | null }) {
  if (!info) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-gray-800 border border-gray-700 text-gray-500 text-xs animate-pulse">
        <Cpu size={13} />
        Detecting compute…
      </div>
    );
  }

  const isGpu = info.using_gpu;

  return (
    <div
      className={`inline-flex items-center gap-2 px-4 py-2 rounded-full border text-xs font-semibold transition-all ${
        isGpu
          ? "bg-green-950/60 border-green-600 text-green-300"
          : "bg-amber-950/60 border-amber-600 text-amber-300"
      }`}
    >
      {isGpu ? (
        <Zap size={14} className="text-green-400" />
      ) : (
        <Cpu size={14} className="text-amber-400" />
      )}

      <span>
        {isGpu ? "GPU Accelerated" : "CPU Mode"}
        {" · "}
        <span className="opacity-80">
          Whisper {info.whisper_model} · {info.whisper_compute_type.toUpperCase()}
        </span>
      </span>

      <span
        className={`ml-1 px-1.5 py-0.5 rounded text-[10px] font-mono ${
          isGpu
            ? "bg-green-900/60 text-green-400"
            : "bg-amber-900/60 text-amber-400"
        }`}
      >
        {info.ffmpeg_encoder === "h264_nvenc" ? "NVENC" : "libx264"}
      </span>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function Processing() {
  const { videoId } = useParams<{ videoId: string }>();
  const navigate = useNavigate();
  const [status, setStatus] = useState<VideoStatus>("pending");
  const [clipCount, setClipCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [sysInfo, setSysInfo] = useState<SystemInfo | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Fetch system info once on mount so the badge shows immediately
  useEffect(() => {
    getSystemInfo()
      .then(setSysInfo)
      .catch(() => setSysInfo(null));
  }, []);

  // Poll video status every 3 s
  useEffect(() => {
    if (!videoId) return;

    const poll = async () => {
      try {
        const data = await getVideoStatus(videoId);
        setStatus(data.status);
        setClipCount(data.clip_count);

        if (data.status === "ready") {
          clearInterval(intervalRef.current!);
          setTimeout(() => navigate(`/review/${videoId}`), 1200);
        } else if (data.status === "failed") {
          clearInterval(intervalRef.current!);
          setError(data.error_message || "Processing failed.");
        }
      } catch {
        setError("Lost connection to server.");
      }
    };

    poll();
    intervalRef.current = setInterval(poll, 3000);
    return () => clearInterval(intervalRef.current!);
  }, [videoId, navigate]);

  const activeStep = getStepIndex(status);

  return (
    <div className="min-h-screen flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-md text-center">
        <h1 className="text-2xl font-bold text-white mb-2">Processing your video</h1>
        <p className="text-gray-400 mb-6 text-sm">
          This usually takes 5–10 minutes for a 20-min video. You can keep this tab open.
        </p>

        {/* ── GPU / CPU Badge ── */}
        <div className="flex justify-center mb-8">
          <ComputeBadge info={sysInfo} />
        </div>

        {/* ── Steps ── */}
        <div className="space-y-4 mb-10 text-left">
          {STEPS.map((step, i) => {
            const isDone = activeStep > i || status === "ready";
            const isActive = activeStep === i && status !== "failed";
            return (
              <div
                key={step.status}
                className={`flex items-center gap-3 p-4 rounded-lg border transition-colors ${
                  isDone
                    ? "border-green-700 bg-green-950/30"
                    : isActive
                    ? "border-red-700 bg-red-950/20"
                    : "border-gray-700 bg-gray-900"
                }`}
              >
                {isDone ? (
                  <CheckCircle size={20} className="text-green-400 shrink-0" />
                ) : isActive ? (
                  <Loader2 size={20} className="text-red-400 animate-spin shrink-0" />
                ) : (
                  <div className="w-5 h-5 rounded-full border border-gray-600 shrink-0" />
                )}
                <span
                  className={`text-sm font-medium ${
                    isDone ? "text-green-300" : isActive ? "text-white" : "text-gray-500"
                  }`}
                >
                  {step.label}
                </span>
              </div>
            );
          })}
        </div>

        {/* ── Error state ── */}
        {error && (
          <div className="flex items-center gap-2 p-4 bg-red-950/40 border border-red-700 rounded-lg text-red-300">
            <XCircle size={20} className="shrink-0" />
            <span className="text-sm">{error}</span>
          </div>
        )}

        {/* ── Clip count when rendering ── */}
        {clipCount > 0 && status === "ready" && (
          <p className="text-gray-400 text-sm mt-4">
            ✓ {clipCount} clips ready — redirecting to review…
          </p>
        )}

        {/* ── Status text ── */}
        <div className="mt-6">
          <span className="text-xs text-gray-600 uppercase tracking-widest">
            Status: {status}
          </span>
        </div>

        {/* ── Detailed compute breakdown (shown when info loaded) ── */}
        {sysInfo && (
          <div className="mt-6 bg-gray-900 border border-gray-800 rounded-lg p-4 text-left">
            <p className="text-xs text-gray-500 uppercase tracking-wider mb-3 font-medium">
              Compute Details
            </p>
            <div className="grid grid-cols-2 gap-y-2 text-xs">
              <span className="text-gray-500">Transcription</span>
              <span className={`font-mono font-semibold ${sysInfo.using_gpu ? "text-green-400" : "text-amber-400"}`}>
                {sysInfo.whisper_device.toUpperCase()} · {sysInfo.whisper_compute_type}
              </span>
              <span className="text-gray-500">Whisper model</span>
              <span className="font-mono text-gray-300">{sysInfo.whisper_model}</span>
              <span className="text-gray-500">Video encoder</span>
              <span className={`font-mono font-semibold ${sysInfo.ffmpeg_encoder === "h264_nvenc" ? "text-green-400" : "text-amber-400"}`}>
                {sysInfo.ffmpeg_encoder}
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
