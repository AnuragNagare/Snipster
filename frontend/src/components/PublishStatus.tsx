import { useEffect, useState } from "react";
import { ExternalLink, RefreshCw, Play } from "lucide-react";

import { getPublishJobs, retryPublishJob } from "../api";
import type { PublishJob } from "../types";

interface Props {
  clipId: string;
}

const statusStyle: Record<string, string> = {
  queued: "text-gray-400",
  publishing: "text-yellow-400",
  published: "text-green-400",
  failed: "text-red-400",
  scheduled: "text-blue-400",
};

export default function PublishStatus({ clipId }: Props) {
  const [jobs, setJobs] = useState<PublishJob[]>([]);

  useEffect(() => {
    let active = true;
    const poll = async () => {
      const data = await getPublishJobs(clipId).catch(() => []);
      if (active) setJobs(data);
    };
    poll();
    const interval = setInterval(poll, 5000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [clipId]);

  const handleRetry = async (jobId: string) => {
    const updated = await retryPublishJob(jobId).catch(() => null);
    if (updated) {
      setJobs((prev) => prev.map((j) => (j.id === jobId ? updated : j)));
    }
  };

  if (jobs.length === 0) return null;

  return (
    <div className="p-4 space-y-2">
      <p className="text-xs text-gray-500 font-medium uppercase tracking-wider mb-3">
        Publish Jobs
      </p>
      {jobs.map((job) => (
        <div
          key={job.id}
          className="flex items-center justify-between bg-gray-800 rounded-lg px-3 py-2"
        >
          <div className="flex items-center gap-2">
            <Play size={13} className="text-red-400" />
            <span className="text-xs text-gray-300 capitalize">{job.platform}</span>
          </div>

          <div className="flex items-center gap-2">
            <span className={`text-xs font-medium ${statusStyle[job.status]}`}>
              {/* "dry-run" is autopilot's rehearsal: nothing was uploaded */}
              {job.platform_post_id === "dry-run" ? "rehearsal (nothing uploaded)" : job.status}
            </span>

            {job.status === "published" && job.platform_post_url && (
              <a
                href={job.platform_post_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-gray-400 hover:text-white"
              >
                <ExternalLink size={13} />
              </a>
            )}

            {job.status === "failed" && (
              <button
                onClick={() => handleRetry(job.id)}
                className="text-gray-500 hover:text-yellow-400"
                title="Retry"
              >
                <RefreshCw size={13} />
              </button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
