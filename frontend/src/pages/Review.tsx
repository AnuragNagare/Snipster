import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { getClipsForVideo, setVideoLanguage } from "../api";
import { useAppStore } from "../store";
import type { Clip } from "../types";
import ClipCard from "../components/ClipCard";
import { languageLabel } from "../utils/language";
import { regenerateCaptionAndWait } from "../utils/regenerate";

const POLL_MS = 3000;
// A caption task can fail for good, so a clip may never settle. Stop asking
// eventually rather than polling this endpoint for the life of the tab.
const MAX_POLL_MS = 10 * 60 * 1000;

/**
 * The backend flips the video to "ready" as soon as the clip rows exist —
 * renders and captions land minutes later. Keep polling until they have.
 */
function allSettled(clips: Clip[]): boolean {
  if (clips.length === 0) return false;
  return clips.every(
    (c) =>
      (c.render_status === "done" || c.render_status === "failed") &&
      Boolean(c.caption)
  );
}

export default function Review() {
  const { videoId } = useParams<{ videoId: string }>();
  const { clips, setClips, updateClip, showToast } = useAppStore();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [polling, setPolling] = useState(true);
  const [languageBusy, setLanguageBusy] = useState(false);
  const [languageChanged, setLanguageChanged] = useState(false);
  const [regeneratingAll, setRegeneratingAll] = useState(false);

  useEffect(() => {
    if (!videoId) return;

    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const startedAt = Date.now();

    const poll = async () => {
      let settled = false;
      try {
        const data = await getClipsForVideo(videoId);
        if (!active) return;
        setClips(data);
        setError(null);
        settled = allSettled(data);
      } catch {
        if (!active) return;
        // Keep polling — a backend restart shouldn't freeze the page for good.
        setError("Failed to load clips.");
      } finally {
        if (active) setLoading(false);
      }

      if (!active) return;
      if (!settled && Date.now() - startedAt < MAX_POLL_MS) {
        timer = setTimeout(poll, POLL_MS);
      } else {
        setPolling(false);
      }
    };

    setLoading(true);
    setPolling(true);
    poll();

    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [videoId, setClips]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="animate-spin text-gray-400" size={40} />
      </div>
    );
  }

  // Only take over the page when there's nothing to show; otherwise keep the
  // last good data on screen while polling retries.
  if (error && clips.length === 0) {
    return (
      <div className="min-h-screen flex items-center justify-center text-red-400">
        {error}
      </div>
    );
  }

  const currentLanguage = clips[0]?.language ?? "";

  const handleLanguage = async (language: "en" | "hi") => {
    if (!videoId || language === currentLanguage) return;
    setLanguageBusy(true);
    try {
      const saved = await setVideoLanguage(videoId, language);
      setClips(clips.map((c) => ({ ...c, language: saved })));
      setLanguageChanged(true);
      showToast(`Language set to ${languageLabel(saved)}.`, "success");
    } catch (err: any) {
      showToast(err?.response?.data?.detail || "Could not change the language.", "error");
    } finally {
      setLanguageBusy(false);
    }
  };

  const handleRegenerateAll = async () => {
    setRegeneratingAll(true);
    const results = await Promise.allSettled(
      clips.map((c) => regenerateCaptionAndWait(c, updateClip))
    );
    setRegeneratingAll(false);
    const arrived = results.filter((r) => r.status === "fulfilled" && r.value).length;
    if (arrived === clips.length) setLanguageChanged(false);
    showToast(
      `${arrived} of ${clips.length} captions regenerated.`,
      arrived === clips.length ? "success" : "info"
    );
  };

  const rendered = clips.filter((c) => c.render_status === "done").length;

  return (
    <div className="min-h-screen p-6 max-w-5xl mx-auto">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-white">Review Clips</h1>
        <p className="text-gray-400 mt-1">
          {clips.length} candidate clip{clips.length !== 1 ? "s" : ""} generated
          {languageLabel(clips[0]?.language) ? ` · ${languageLabel(clips[0]?.language)} video` : ""}.
          Approve to publish, or edit before approving.
        </p>

        {clips.length > 0 && (
          <div className="flex flex-wrap items-center gap-3 mt-3 text-sm">
            <label className="flex items-center gap-2 text-gray-400">
              Spoken language
              <select
                value={currentLanguage}
                disabled={languageBusy || regeneratingAll}
                onChange={(e) => handleLanguage(e.target.value as "en" | "hi")}
                className="bg-gray-800 border border-gray-600 rounded px-2 py-1 text-white text-sm"
              >
                {!["en", "hi"].includes(currentLanguage) && (
                  <option value={currentLanguage} disabled>
                    {languageLabel(currentLanguage) ?? "Unknown"}
                  </option>
                )}
                <option value="en">English</option>
                <option value="hi">Hindi</option>
              </select>
            </label>
            {languageChanged && (
              <>
                <button
                  onClick={handleRegenerateAll}
                  disabled={regeneratingAll}
                  className="flex items-center gap-1.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white text-xs font-medium px-3 py-1.5 rounded-lg"
                >
                  {regeneratingAll && <Loader2 size={12} className="animate-spin" />}
                  {regeneratingAll ? "Regenerating…" : "Regenerate all captions"}
                </button>
                <span className="text-xs text-gray-500">
                  Existing captions and rendered clips keep the old language until regenerated or
                  re-rendered. This does not re-transcribe the audio.
                </span>
              </>
            )}
          </div>
        )}

        {polling && (
          <p className="flex items-center gap-2 text-sm text-gray-500 mt-3">
            <Loader2 size={14} className="animate-spin" />
            Rendering &amp; captioning — {rendered} of {clips.length} ready. This
            page updates itself.
          </p>
        )}
      </div>

      {clips.length === 0 ? (
        <div className="text-center py-20 text-gray-500">
          No clips generated yet. Check back in a moment.
        </div>
      ) : (
        <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
          {clips.map((clip) => (
            <ClipCard key={clip.id} clip={clip} />
          ))}
        </div>
      )}
    </div>
  );
}
