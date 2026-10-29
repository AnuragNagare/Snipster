import { useState } from "react";
import { X, RotateCcw } from "lucide-react";
import { updateClip, rerenderClip } from "../api";
import { useAppStore } from "../store";
import type { Clip } from "../types";

interface Props {
  clip: Clip;
  onClose: () => void;
}

export default function ClipEditor({ clip, onClose }: Props) {
  const { updateClip: storeUpdate, showToast } = useAppStore();
  const [caption, setCaption] = useState(clip.caption || "");
  const [hashtags, setHashtags] = useState(clip.hashtags || "");
  const [startTime, setStartTime] = useState(clip.start_time);
  const [endTime, setEndTime] = useState(clip.end_time);
  const [loading, setLoading] = useState(false);

  const duration = (endTime - startTime).toFixed(1);
  const isValidDuration = endTime - startTime >= 5 && endTime - startTime <= 90;

  const handleSave = async () => {
    setLoading(true);
    try {
      const updated = await updateClip(clip.id, { caption, hashtags });
      storeUpdate(updated);
      showToast("Clip updated.", "success");
      onClose();
    } catch {
      showToast("Failed to save changes.", "error");
    } finally {
      setLoading(false);
    }
  };

  const handleRerender = async () => {
    if (!isValidDuration) {
      showToast("Duration must be between 5 and 90 seconds.", "error");
      return;
    }
    setLoading(true);
    try {
      const updated = await rerenderClip(clip.id, startTime, endTime);
      storeUpdate(updated);
      showToast("Re-render queued!", "success");
      onClose();
    } catch {
      showToast("Failed to queue re-render.", "error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-lg">
        {/* Header */}
        <div className="flex items-center justify-between p-5 border-b border-gray-700">
          <h2 className="text-white font-semibold">Edit Clip</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-white">
            <X size={20} />
          </button>
        </div>

        <div className="p-5 space-y-5">
          {/* Caption */}
          <div>
            <label className="block text-sm text-gray-400 mb-1.5">Caption</label>
            <textarea
              value={caption}
              onChange={(e) => setCaption(e.target.value)}
              rows={3}
              className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-red-500 resize-none"
              placeholder="Enter clip caption…"
            />
          </div>

          {/* Hashtags */}
          <div>
            <label className="block text-sm text-gray-400 mb-1.5">Hashtags</label>
            <input
              value={hashtags}
              onChange={(e) => setHashtags(e.target.value)}
              className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-red-500"
              placeholder="#hashtag1 #hashtag2…"
            />
          </div>

          {/* Trim Points */}
          <div>
            <label className="block text-sm text-gray-400 mb-3">
              Trim Points
              <span className={`ml-2 text-xs px-2 py-0.5 rounded ${isValidDuration ? "bg-green-900 text-green-300" : "bg-red-900 text-red-400"}`}>
                {duration}s {!isValidDuration && "(must be 5–90s)"}
              </span>
            </label>
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <span className="text-xs text-gray-500 w-12 text-right">Start</span>
                <input
                  type="range"
                  min={0}
                  max={clip.end_time + 30}
                  step={0.5}
                  value={startTime}
                  onChange={(e) => setStartTime(Number(e.target.value))}
                  className="flex-1 accent-red-500"
                />
                <span className="text-xs text-gray-300 w-14">{startTime.toFixed(1)}s</span>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-gray-500 w-12 text-right">End</span>
                <input
                  type="range"
                  min={startTime + 5}
                  max={clip.end_time + 30}
                  step={0.5}
                  value={endTime}
                  onChange={(e) => setEndTime(Number(e.target.value))}
                  className="flex-1 accent-red-500"
                />
                <span className="text-xs text-gray-300 w-14">{endTime.toFixed(1)}s</span>
              </div>
            </div>
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-3 p-5 border-t border-gray-700">
          <button
            onClick={handleSave}
            disabled={loading}
            className="flex-1 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white text-sm font-medium py-2.5 rounded-lg transition-colors"
          >
            Save Caption Only
          </button>
          <button
            onClick={handleRerender}
            disabled={loading || !isValidDuration}
            className="flex-1 bg-red-600 hover:bg-red-500 disabled:bg-gray-700 disabled:opacity-50 text-white text-sm font-medium py-2.5 rounded-lg transition-colors flex items-center justify-center gap-1.5"
          >
            <RotateCcw size={14} />
            Save & Re-render
          </button>
        </div>
      </div>
    </div>
  );
}
