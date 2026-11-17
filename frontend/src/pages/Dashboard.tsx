import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Eye, ExternalLink, Loader2, MessageCircle, RefreshCw, ThumbsUp, Video } from "lucide-react";
import { getDashboard } from "../api";
import type { DashboardPost, DashboardResponse } from "../types";
import { languageLabel } from "../utils/language";

const REFRESH_MS = 60_000;

const num = (n?: number | null) => (n === null || n === undefined ? "–" : n.toLocaleString());

const when = (iso?: string) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";

function Stat({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="bg-gray-900 border border-gray-700 rounded-xl p-4">
      <div className="flex items-center gap-2 text-xs text-gray-400">
        {icon} {label}
      </div>
      <div className="mt-1 text-2xl font-bold text-white">{value}</div>
    </div>
  );
}

function PostRow({ post }: { post: DashboardPost }) {
  const lang = languageLabel(post.language);
  return (
    <li className="py-3 flex flex-col gap-1.5 sm:flex-row sm:items-center sm:gap-4">
      <div className="min-w-0 flex-1">
        <p lang={post.language} className="text-sm text-gray-200 leading-snug line-clamp-2">
          {post.caption || "(no caption)"}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500">
          <span>{when(post.published_at)}</span>
          {post.source_title && <span className="truncate max-w-[16rem]">From: {post.source_title}</span>}
          {lang && <span className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">{lang}</span>}
          <span className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">{post.autopilot ? "autopilot" : "manual"}</span>
          <a
            href={post.url}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1 text-blue-400 hover:underline"
          >
            Open <ExternalLink size={11} />
          </a>
        </div>
      </div>
      {post.has_stats ? (
        <div className="flex gap-5 text-sm shrink-0">
          <span className="flex items-center gap-1.5 text-white" title="Views">
            <Eye size={14} className="text-gray-400" /> {num(post.views)}
          </span>
          <span className="flex items-center gap-1.5 text-gray-300" title="Likes">
            <ThumbsUp size={14} className="text-gray-500" /> {num(post.likes)}
          </span>
          <span className="flex items-center gap-1.5 text-gray-300" title="Comments">
            <MessageCircle size={14} className="text-gray-500" /> {num(post.comments)}
          </span>
        </div>
      ) : (
        <span className="text-xs text-gray-500 shrink-0">No numbers yet</span>
      )}
    </li>
  );
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      setData(await getDashboard());
      setError(false);
    } catch {
      setError(true);
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  if (!data) {
    return (
      <div className="min-h-[60vh] flex items-center justify-center">
        {error ? <p className="text-red-400">Could not load the dashboard.</p> : <Loader2 className="animate-spin text-gray-400" size={36} />}
      </div>
    );
  }

  const { totals, posts } = data;
  const withoutNumbers = totals.posts - totals.with_stats;

  return (
    <div className="p-6 max-w-4xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold text-white">Dashboard</h1>
        <button onClick={load} disabled={refreshing} className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-white disabled:opacity-50">
          <RefreshCw size={14} className={refreshing ? "animate-spin" : ""} /> Refresh
        </button>
      </div>
      {error && <p className="text-sm text-amber-300">Could not refresh. Showing the last data.</p>}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat icon={<Video size={14} />} label="Posts" value={num(totals.posts)} />
        <Stat icon={<Eye size={14} />} label="Views" value={num(totals.views)} />
        <Stat icon={<ThumbsUp size={14} />} label="Likes" value={num(totals.likes)} />
        <Stat icon={<MessageCircle size={14} />} label="Comments" value={num(totals.comments)} />
      </div>

      {data.stats_error && (
        <p className="text-sm text-amber-300 bg-amber-950/30 border border-amber-800 rounded-lg px-3 py-2">
          The numbers could not be loaded: {data.stats_error}
        </p>
      )}
      {!data.stats_error && withoutNumbers > 0 && (
        <p className="text-xs text-gray-500">
          {withoutNumbers} post{withoutNumbers !== 1 ? "s have" : " has"} no numbers: YouTube only shares them for
          public or unlisted videos. Change the visibility on the <Link to="/activity" className="underline">Activity</Link> page.
          Totals count only posts with numbers.
        </p>
      )}
      {data.rehearsals > 0 && (
        <p className="text-xs text-gray-500">
          {data.rehearsals} dry-run post{data.rehearsals !== 1 ? "s are" : " is"} not shown: nothing was uploaded.
        </p>
      )}

      <section>
        <h2 className="text-white font-semibold mb-2">Posts</h2>
        {posts.length === 0 ? (
          <p className="text-sm text-gray-500">Nothing has been posted to YouTube yet.</p>
        ) : (
          <ul className="divide-y divide-gray-800">
            {posts.map((p) => (
              <PostRow key={p.id} post={p} />
            ))}
          </ul>
        )}
        <p className="mt-4 text-xs text-gray-600">
          Numbers come from YouTube and can lag by a few hours. This page refreshes every minute.
        </p>
      </section>
    </div>
  );
}
