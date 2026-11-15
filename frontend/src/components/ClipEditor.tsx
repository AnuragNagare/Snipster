import { useEffect, useState } from "react";
import { X, RotateCcw, Plus, Trash2, Loader2 } from "lucide-react";
import { updateClip, rerenderClip, getSubtitles, updateSubtitles } from "../api";
import { useAppStore } from "../store";
import type { Clip, SubtitleLine } from "../types";

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

  const [tab, setTab] = useState<"details" | "subtitles">("details");
  const [lines, setLines] = useState<SubtitleLine[] | null>(null); // null = not loaded yet
  const [customized, setCustomized] = useState(false);
  const [subsError, setSubsError] = useState(false);

  // Load the subtitle lines the first time the Subtitles tab is opened.
  useEffect(() => {
    if (tab !== "subtitles" || lines !== null) return;
    let active = true;
    getSubtitles(clip.id)
      .then((r) => {
        if (!active) return;
        setLines(r.lines);
        setCustomized(r.customized);
      })
      .catch(() => {
        if (active) setSubsError(true);
      });
    return () => {
      active = false;
    };
  }, [tab, lines, clip.id]);

  const duration = (endTime - startTime).toFixed(1);
  const isValidDuration = endTime - startTime >= 5 && endTime - startTime <= 90;
  const trimChanged = startTime !== clip.start_time || endTime !== clip.end_time;
  const clipLength = clip.end_time - clip.start_time;

  const setLine = (i: number, patch: Partial<SubtitleLine>) =>
    setLines((cur) => (cur ? cur.map((l, j) => (j === i ? { ...l, ...patch } : l)) : cur));
  const removeLine = (i: number) =>
    setLines((cur) => (cur ? cur.filter((_, j) => j !== i) : cur));
  const addLine = () =>
    setLines((cur) => {
      const last = cur && cur.length ? cur[cur.length - 1].end : 0;
      const start = Math.min(last, Math.max(0, clipLength - 1));
      return [...(cur ?? []), { start, end: Math.min(start + 2, clipLength), text: "" }];
    });

  const linesValid =
    lines !== null &&
    lines.every(
      (l) => l.text.trim() !== "" && l.end > l.start && l.start >= 0 && l.end <= clipLength + 0.5
    );

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

  const handleSubtitleSave = async (rerender: boolean) => {
    if (!lines) return;
    setLoading(true);
    try {
      const saved = await updateSubtitles(
        clip.id,
        lines.map((l) => ({ ...l, text: l.text.trim() }))
      );
      setLines(saved.lines);
      setCustomized(true);
    } catch (err: any) {
      showToast(err?.response?.data?.detail || "Failed to save subtitles.", "error");
      setLoading(false);
      return;
    }
    try {
      if (rerender) {
        // The saved trim, not the sliders: the subtitles are timed against it.
        const updated = await rerenderClip(clip.id, clip.start_time, clip.end_time);
        storeUpdate(updated);
        showToast("Subtitles saved. Re-render queued!", "success");
        onClose();
      } else {
        showToast("Subtitles saved. They appear after the next re-render.", "success");
      }
    } catch {
      showToast("Subtitles saved, but the re-render could not be queued.", "error");
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

        {/* Tabs */}
        <div className="flex border-b border-gray-700 px-5">
          {(["details", "subtitles"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
                tab === t
                  ? "border-red-500 text-white"
                  : "border-transparent text-gray-400 hover:text-white"
              }`}
            >
              {t === "details" ? "Details" : "Subtitles"}
            </button>
          ))}
        </div>

        {tab === "details" ? (
          <>
            <div className="p-5 space-y-5">
              {/* Caption */}
              <div>
                <label className="block text-sm text-gray-400 mb-1.5">Caption</label>
                <textarea
                  value={caption}
                  lang={clip.language}
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
                  lang={clip.language}
                  onChange={(e) => setHashtags(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-red-500"
                  placeholder="#hashtag1 #hashtag2…"
                />
              </div>

              {/* Trim Points */}
              <div>
                <label className="block text-sm text-gray-400 mb-3">
                  Trim Points
                  <span
                    className={`ml-2 text-xs px-2 py-0.5 rounded ${
                      isValidDuration ? "bg-green-900 text-green-300" : "bg-red-900 text-red-400"
                    }`}
                  >
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
            {trimChanged && (
              <p className="px-5 pb-4 -mt-2 text-xs text-amber-400">
                Changing the trim points discards any subtitles you edited for this clip.
              </p>
            )}
          </>
        ) : (
          <>
            <div className="p-5 space-y-3 max-h-[26rem] overflow-y-auto">
              <p className="text-xs text-gray-500">
                {customized
                  ? "You have edited these subtitles."
                  : "Generated from the transcript. Fix any wrong words, then re-render to burn them in."}{" "}
                Times are seconds from the start of the clip (0–{clipLength.toFixed(1)}).
              </p>
              {subsError && <p className="text-sm text-red-400">Could not load subtitles.</p>}
              {lines === null && !subsError && (
                <div className="flex justify-center py-8">
                  <Loader2 className="animate-spin text-gray-400" size={24} />
                </div>
              )}
              {lines?.length === 0 && (
                <p className="text-sm text-gray-500">
                  No subtitles. The clip will render without any.
                </p>
              )}
              {lines?.map((l, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    type="number"
                    min={0}
                    step={0.1}
                    value={l.start}
                    onChange={(e) => setLine(i, { start: Number(e.target.value) })}
                    className="w-16 bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-white text-xs"
                    aria-label="Start (s)"
                  />
                  <input
                    type="number"
                    min={0}
                    step={0.1}
                    value={l.end}
                    onChange={(e) => setLine(i, { end: Number(e.target.value) })}
                    className="w-16 bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-white text-xs"
                    aria-label="End (s)"
                  />
                  <input
                    value={l.text}
                    lang={clip.language}
                    maxLength={200}
                    onChange={(e) => setLine(i, { text: e.target.value })}
                    className="flex-1 min-w-0 bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-white text-sm focus:outline-none focus:border-red-500"
                    aria-label="Subtitle text"
                  />
                  <button
                    onClick={() => removeLine(i)}
                    className="text-gray-500 hover:text-red-400"
                    aria-label="Delete line"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              ))}
              {lines !== null && (
                <button
                  onClick={addLine}
                  className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white"
                >
                  <Plus size={14} /> Add line
                </button>
              )}
              {lines !== null && !linesValid && (
                <p className="text-xs text-amber-400">
                  Every line needs text, and must end after it starts, inside the clip.
                </p>
              )}
            </div>
            <div className="flex gap-3 p-5 border-t border-gray-700">
              <button
                onClick={() => handleSubtitleSave(false)}
                disabled={loading || !linesValid}
                className="flex-1 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white text-sm font-medium py-2.5 rounded-lg transition-colors"
              >
                Save Subtitles
              </button>
              <button
                onClick={() => handleSubtitleSave(true)}
                disabled={loading || !linesValid}
                className="flex-1 bg-red-600 hover:bg-red-500 disabled:bg-gray-700 disabled:opacity-50 text-white text-sm font-medium py-2.5 rounded-lg transition-colors flex items-center justify-center gap-1.5"
              >
                <RotateCcw size={14} />
                Save & Re-render
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
