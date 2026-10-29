import { useState } from "react";
import {
  CheckCircle,
  XCircle,
  Edit2,
  Play,
  Camera,
  Clock,
  Loader2,
} from "lucide-react";

import { approveClip, rejectClip, publishClip } from "../api";
import { useAppStore } from "../store";
import type { Clip, Platform } from "../types";
import ClipEditor from "./ClipEditor";
import PublishStatus from "./PublishStatus";

interface Props {
  clip: Clip;
}

const duration = (c: Clip) => (c.end_time - c.start_time).toFixed(1);

const statusColors: Record<string, string> = {
  pending: "bg-gray-700 text-gray-300",
  approved: "bg-green-800 text-green-300",
  rejected: "bg-red-900 text-red-400",
};

export default function ClipCard({ clip }: Props) {
  const { updateClip, showToast, accounts } = useAppStore();
  const [editing, setEditing] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [showPublishJobs, setShowPublishJobs] = useState(false);

  const handleApprove = async () => {
    try {
      const updated = await approveClip(clip.id);
      updateClip(updated);
      showToast("Clip approved!", "success");
    } catch {
      showToast("Failed to approve clip.", "error");
    }
  };

  const handleReject = async () => {
    try {
      const updated = await rejectClip(clip.id);
      updateClip(updated);
    } catch {
      showToast("Failed to reject clip.", "error");
    }
  };

  const handlePublish = async (platforms: Platform[]) => {
    setPublishing(true);
    try {
      await publishClip(clip.id, platforms);
      showToast(`Publishing to ${platforms.join(" & ")}…`, "success");
      setShowPublishJobs(true);
    } catch (err: any) {
      showToast(err?.response?.data?.detail || "Publish failed.", "error");
    } finally {
      setPublishing(false);
    }
  };

  return (
    <div className="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden flex flex-col">
      {/* Video Preview */}
      <div className="relative bg-black aspect-[9/16] max-h-64 overflow-hidden">
        {clip.preview_url ? (
          <video
            src={clip.preview_url}
            className="w-full h-full object-contain"
            controls
            preload="metadata"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            {clip.render_status === "failed" ? (
              <div className="text-center text-red-400">
                <XCircle size={28} className="mx-auto mb-2" />
                <p className="text-xs">Render failed</p>
              </div>
            ) : clip.render_status === "rendering" ? (
              <div className="text-center text-gray-500">
                <Loader2 className="animate-spin mx-auto mb-2" size={28} />
                <p className="text-xs">Rendering…</p>
              </div>
            ) : (
              <div className="text-center text-gray-600">
                <Clock size={28} className="mx-auto mb-2" />
                <p className="text-xs">Pending render</p>
              </div>
            )}
          </div>
        )}

        {/* Duration badge */}
        <span className="absolute bottom-2 right-2 bg-black/70 text-white text-xs px-2 py-0.5 rounded">
          {duration(clip)}s
        </span>

        {/* Approval status badge */}
        <span
          className={`absolute top-2 left-2 text-xs px-2 py-0.5 rounded font-medium ${statusColors[clip.approval_status]}`}
        >
          {clip.approval_status}
        </span>
      </div>

      {/* Caption & Hashtags */}
      <div className="p-4 flex-1 space-y-2">
        {clip.caption ? (
          <p className="text-sm text-gray-200 leading-snug">{clip.caption}</p>
        ) : (
          <p className="text-sm text-gray-500 italic">Caption generating…</p>
        )}
        {clip.hashtags && (
          <p className="text-xs text-blue-400">{clip.hashtags}</p>
        )}
        {clip.llm_reason && (
          <p className="text-xs text-gray-500 border-t border-gray-800 pt-2">
            💡 {clip.llm_reason}
          </p>
        )}
      </div>

      {/* Actions */}
      {clip.approval_status === "pending" && (
        <div className="p-4 pt-0 grid grid-cols-3 gap-2">
          <button
            onClick={handleApprove}
            className="flex items-center justify-center gap-1 bg-green-700 hover:bg-green-600 text-white text-xs font-medium py-2 rounded-lg transition-colors"
          >
            <CheckCircle size={14} /> Approve
          </button>
          <button
            onClick={() => setEditing(true)}
            className="flex items-center justify-center gap-1 bg-gray-700 hover:bg-gray-600 text-white text-xs font-medium py-2 rounded-lg transition-colors"
          >
            <Edit2 size={14} /> Edit
          </button>
          <button
            onClick={handleReject}
            className="flex items-center justify-center gap-1 bg-gray-800 hover:bg-red-900 text-gray-400 hover:text-red-400 text-xs font-medium py-2 rounded-lg transition-colors"
          >
            <XCircle size={14} /> Reject
          </button>
        </div>
      )}

      {/* Publish buttons (approved clips) */}
      {clip.approval_status === "approved" && (
        <div className="p-4 pt-0 space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={() => handlePublish(["youtube"])}
              disabled={publishing || !accounts?.youtube_connected}
              className="flex items-center justify-center gap-1 bg-red-700 hover:bg-red-600 disabled:bg-gray-800 disabled:text-gray-600 text-white text-xs font-medium py-2 rounded-lg transition-colors"
            >
              <Play size={13} /> Shorts
            </button>
            <button
              onClick={() => handlePublish(["instagram"])}
              disabled={publishing || !accounts?.instagram_connected}
              className="flex items-center justify-center gap-1 bg-pink-700 hover:bg-pink-600 disabled:bg-gray-800 disabled:text-gray-600 text-white text-xs font-medium py-2 rounded-lg transition-colors"
            >
              <Camera size={13} /> Reels
            </button>
          </div>
          <button
            onClick={() => handlePublish(["youtube", "instagram"])}
            disabled={publishing || (!accounts?.youtube_connected && !accounts?.instagram_connected)}
            className="w-full flex items-center justify-center gap-1 bg-gray-700 hover:bg-gray-600 disabled:bg-gray-800 disabled:text-gray-600 text-white text-xs font-medium py-2 rounded-lg transition-colors"
          >
            Publish Both
          </button>
        </div>
      )}

      {/* Publish job status */}
      {showPublishJobs && (
        <div className="border-t border-gray-800">
          <PublishStatus clipId={clip.id} />
        </div>
      )}

      {/* Editor modal */}
      {editing && (
        <ClipEditor clip={clip} onClose={() => setEditing(false)} />
      )}
    </div>
  );
}
