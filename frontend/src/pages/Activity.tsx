import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ExternalLink, Loader2, Play, RefreshCw, RotateCcw } from "lucide-react";
import {
  getActivity,
  getAutopilotSettings,
  retryPublishJob,
  runAutopilotNow,
  updateAutopilotSettings,
} from "../api";
import { useAppStore } from "../store";
import type {
  ActivityJob,
  ActivityResponse,
  AutopilotSettings,
  AutopilotSettingsUpdate,
} from "../types";
import { languageLabel } from "../utils/language";

const REFRESH_MS = 15_000;

const when = (iso?: string) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";

/** "never", "3 min ago", "2 h ago" */
function ago(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

// ─── Settings card ────────────────────────────────────────────────────────────

function Toggle({
  label,
  hint,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-start gap-3 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-1 accent-red-500"
      />
      <span>
        <span className="text-sm text-white">{label}</span>
        {hint && <span className="block text-xs text-gray-500">{hint}</span>}
      </span>
    </label>
  );
}

function SettingsCard({
  settings,
  onChange,
}: {
  settings: AutopilotSettings;
  onChange: (s: AutopilotSettings) => void;
}) {
  const { showToast } = useAppStore();
  const [busy, setBusy] = useState(false);
  const [runTimes, setRunTimes] = useState(settings.run_times);
  const [timezone, setTimezone] = useState(settings.timezone);
  const [perDay, setPerDay] = useState(String(settings.posts_per_day));
  const [perVideo, setPerVideo] = useState(String(settings.clips_per_video));

  const save = async (changes: AutopilotSettingsUpdate) => {
    setBusy(true);
    try {
      onChange(await updateAutopilotSettings(changes));
      return true;
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      showToast(typeof detail === "string" ? detail : "Could not save.", "error");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const runNow = async () => {
    setBusy(true);
    try {
      showToast(await runAutopilotNow(), "success");
    } catch (err: any) {
      showToast(err?.response?.data?.detail || "Could not start a run.", "error");
    } finally {
      setBusy(false);
    }
  };

  const seen = settings.seconds_since_tick;
  const workerOk = seen !== null && seen < Math.max(15, settings.tick_minutes * 3) * 60;
  const live = settings.enabled && !settings.dry_run;

  return (
    <section className="bg-gray-900 border border-gray-700 rounded-xl p-5 space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-white font-semibold">Autopilot</h2>
          <p className="text-xs text-gray-500 mt-0.5">
            {settings.posted_today} posted today · {settings.scheduled_count} waiting
          </p>
        </div>
        <span
          className={`text-xs px-2.5 py-1 rounded-full border ${
            workerOk
              ? "border-green-700 text-green-300 bg-green-950/40"
              : "border-amber-700 text-amber-300 bg-amber-950/40"
          }`}
          title="Autopilot only runs while the worker and the scheduler are running on your PC."
        >
          {workerOk ? `Scheduler seen ${ago(seen)}` : `Scheduler not seen (${ago(seen)})`}
        </span>
      </div>

      {!workerOk && (
        <p className="flex items-start gap-2 text-xs text-amber-300">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          Nothing runs while the PC is off or the scheduler (<code>celery … beat</code>) and the worker
          are not running. Posts you scheduled wait until they are back.
        </p>
      )}
      {!settings.youtube_connected && (
        <p className="text-xs text-amber-300">
          YouTube is not connected. <Link to="/settings" className="underline">Connect it in Settings.</Link>
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        <Toggle
          label="Autopilot on"
          hint="The kill switch. Off: nothing is picked, approved or posted automatically."
          checked={settings.enabled}
          disabled={busy}
          onChange={(v) => save({ enabled: v })}
        />
        <Toggle
          label="Dry run"
          hint="Does everything except upload. Posts show as a rehearsal. Keep this on until the results look right."
          checked={settings.dry_run}
          disabled={busy}
          onChange={(v) => save({ dry_run: v })}
        />
        <Toggle
          label="Approve clips automatically"
          hint="Off: clips wait for your review on the Review page."
          checked={settings.auto_approve}
          disabled={busy}
          onChange={(v) => save({ auto_approve: v })}
        />
        <label className="text-sm text-white">
          Upload visibility
          <select
            value={settings.privacy}
            disabled={busy}
            onChange={(e) => save({ privacy: e.target.value as AutopilotSettings["privacy"] })}
            className="mt-1 block w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm"
          >
            <option value="private">Private (only you)</option>
            <option value="unlisted">Unlisted (anyone with the link)</option>
            <option value="public">Public</option>
          </select>
          <span className="block text-xs text-gray-500 mt-1">Applies to every upload, including ones you start by hand.</span>
        </label>
      </div>

      <div className="grid gap-4 sm:grid-cols-4">
        <label className="text-xs text-gray-400">
          Posts per day
          <input
            type="number" min={1} max={10} value={perDay}
            onChange={(e) => setPerDay(e.target.value)}
            className="mt-1 block w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-white"
          />
        </label>
        <label className="text-xs text-gray-400">
          Clips per video
          <input
            type="number" min={1} max={5} value={perVideo}
            onChange={(e) => setPerVideo(e.target.value)}
            className="mt-1 block w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-white"
          />
        </label>
        <label className="text-xs text-gray-400">
          Pick a video at (hours)
          <input
            value={runTimes}
            onChange={(e) => setRunTimes(e.target.value)}
            placeholder="9,18"
            className="mt-1 block w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-white"
          />
        </label>
        <label className="text-xs text-gray-400">
          Timezone
          <input
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            placeholder="Asia/Kolkata"
            className="mt-1 block w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-sm text-white"
          />
        </label>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          disabled={busy}
          onClick={async () => {
            const ok = await save({
              posts_per_day: Number(perDay),
              clips_per_video: Number(perVideo),
              run_times: runTimes,
              timezone,
            });
            if (ok) showToast("Saved.", "success");
          }}
          className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded-lg"
        >
          Save schedule
        </button>
        <button
          disabled={busy || !settings.enabled}
          onClick={runNow}
          title={settings.enabled ? "" : "Turn autopilot on first"}
          className="flex items-center gap-1.5 bg-red-600 hover:bg-red-500 disabled:bg-gray-700 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded-lg"
        >
          <Play size={14} /> Run now
        </button>
        {live && (
          <span className="text-xs text-amber-300">
            Live: clips are uploaded as {settings.privacy}.
          </span>
        )}
      </div>
    </section>
  );
}

// ─── Job rows ─────────────────────────────────────────────────────────────────

function JobRow({ job, onRetry, busy }: { job: ActivityJob; onRetry: (id: string) => void; busy: boolean }) {
  const label = languageLabel(job.language);
  const status =
    job.status === "published" && job.dry_run ? "rehearsal" : job.status;
  const color: Record<string, string> = {
    published: "text-green-400",
    rehearsal: "text-sky-300",
    scheduled: "text-blue-400",
    queued: "text-gray-300",
    publishing: "text-yellow-400",
    failed: "text-red-400",
  };
  const time =
    job.status === "scheduled" ? `goes out ${when(job.scheduled_at)}` : when(job.published_at || job.created_at);

  return (
    <li className="py-3 flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={`text-xs font-semibold uppercase tracking-wide ${color[status] ?? "text-gray-400"}`}>
          {status}
        </span>
        <span className="text-xs text-gray-500">{time}</span>
        {job.autopilot ? (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">autopilot</span>
        ) : (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">manual</span>
        )}
        {label && <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">{label}</span>}
        {job.post_url && (
          <a
            href={job.post_url}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1 text-xs text-blue-400 hover:underline"
          >
            Open on YouTube <ExternalLink size={11} />
          </a>
        )}
      </div>
      <p lang={job.language} className="text-sm text-gray-200 leading-snug">
        {job.caption || "(no caption)"}
      </p>
      {job.source_title && <p className="text-xs text-gray-500">From: {job.source_title}</p>}
      {job.status === "failed" && (
        <div className="flex flex-wrap items-center gap-3 mt-1">
          {job.error && <p className="text-xs text-red-300">{job.error}</p>}
          {job.needs_reconnect ? (
            <Link to="/settings" className="text-xs text-amber-300 underline">
              Reconnect YouTube, then retry
            </Link>
          ) : null}
          <button
            disabled={busy}
            onClick={() => onRetry(job.id)}
            className="flex items-center gap-1 text-xs bg-gray-800 hover:bg-gray-700 disabled:opacity-50 text-white px-2.5 py-1 rounded"
          >
            <RotateCcw size={11} /> Retry
          </button>
        </div>
      )}
    </li>
  );
}

const waiting = (j: ActivityJob) => ["scheduled", "queued", "publishing"].includes(j.status);

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function Activity() {
  const { showToast } = useAppStore();
  const [settings, setSettings] = useState<AutopilotSettings | null>(null);
  const [activity, setActivity] = useState<ActivityResponse | null>(null);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([getAutopilotSettings(), getActivity(60)]);
      setSettings(s);
      setActivity(a);
      setError(false);
    } catch {
      setError(true);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  const retry = async (jobId: string) => {
    setBusy(true);
    try {
      await retryPublishJob(jobId);
      showToast("Retry queued.", "success");
      await load();
    } catch (err: any) {
      showToast(err?.response?.data?.detail || "Could not retry.", "error");
    } finally {
      setBusy(false);
    }
  };

  if (!settings || !activity) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center">
        {error ? (
          <p className="text-red-400">Could not load activity.</p>
        ) : (
          <Loader2 className="animate-spin text-gray-400" size={36} />
        )}
      </div>
    );
  }

  const upcoming = activity.jobs.filter(waiting).sort((a, b) => (a.scheduled_at ?? "").localeCompare(b.scheduled_at ?? ""));
  const history = activity.jobs.filter((j) => !waiting(j));

  return (
    <div className="p-6 max-w-4xl mx-auto space-y-8">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-white">Activity</h1>
        <button onClick={load} className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-white">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
      {error && <p className="text-sm text-amber-300">Could not refresh. Showing the last data.</p>}

      <SettingsCard settings={settings} onChange={setSettings} />

      <section>
        <h2 className="text-white font-semibold mb-2">Coming up ({upcoming.length})</h2>
        {upcoming.length === 0 ? (
          <p className="text-sm text-gray-500">Nothing is waiting to be posted.</p>
        ) : (
          <ul className="divide-y divide-gray-800">
            {upcoming.map((j) => (
              <JobRow key={j.id} job={j} onRetry={retry} busy={busy} />
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2 className="text-white font-semibold mb-2">Posted and failed</h2>
        {history.length === 0 ? (
          <p className="text-sm text-gray-500">Nothing has been posted yet.</p>
        ) : (
          <ul className="divide-y divide-gray-800">
            {history.map((j) => (
              <JobRow key={j.id} job={j} onRetry={retry} busy={busy} />
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2 className="text-white font-semibold mb-2">Videos picked from your channel</h2>
        {activity.sources.length === 0 ? (
          <p className="text-sm text-gray-500">Autopilot has not picked a video yet.</p>
        ) : (
          <ul className="divide-y divide-gray-800">
            {activity.sources.map((s) => (
              <li key={s.id} className="py-2 flex flex-wrap items-baseline gap-x-3">
                <span
                  className={`text-xs font-semibold uppercase ${
                    s.status === "failed" ? "text-red-400" : s.status === "done" ? "text-green-400" : "text-yellow-400"
                  }`}
                >
                  {s.status}
                </span>
                <a
                  href={`https://www.youtube.com/watch?v=${s.youtube_video_id}`}
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm text-gray-200 hover:underline"
                >
                  {s.title || s.youtube_video_id}
                </a>
                <span className="text-xs text-gray-500">{when(s.picked_at)}</span>
                {s.reason && <span className="text-xs text-gray-500 basis-full">{s.reason}</span>}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
