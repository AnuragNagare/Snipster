import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { getClipsForVideo } from "../api";
import { useAppStore } from "../store";
import type { Clip } from "../types";
import ClipCard from "../components/ClipCard";

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
  const { clips, setClips } = useAppStore();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [polling, setPolling] = useState(true);

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

  const rendered = clips.filter((c) => c.render_status === "done").length;

  return (
    <div className="min-h-screen p-6 max-w-5xl mx-auto">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-white">Review Clips</h1>
        <p className="text-gray-400 mt-1">
          {clips.length} candidate clip{clips.length !== 1 ? "s" : ""} generated.
          Approve to publish, or edit before approving.
        </p>

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
