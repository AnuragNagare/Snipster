import { getClip, regenerateCaption } from "../api";
import type { Clip } from "../types";

const POLL_MS = 3000;
const MAX_WAIT_MS = 90_000;

/**
 * Ask the worker for a fresh caption, then wait until the clip row changes.
 * The task runs on another machine, so the new text is not in the queue response:
 * the clip's `updated_at` moving is the sign it has landed.
 *
 * Resolves true once the new caption arrived (and was passed to `onUpdate`),
 * false if it had not arrived after 90 s (the worker may be off).
 */
export async function regenerateCaptionAndWait(
  clip: Clip,
  onUpdate: (clip: Clip) => void
): Promise<boolean> {
  await regenerateCaption(clip.id);
  const started = Date.now();
  while (Date.now() - started < MAX_WAIT_MS) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    const fresh = await getClip(clip.id);
    if (fresh.updated_at !== clip.updated_at) {
      onUpdate(fresh);
      return true;
    }
  }
  return false;
}
