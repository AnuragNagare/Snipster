import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Upload, Link, ShieldCheck } from "lucide-react";
import { uploadVideo, submitVideoUrl } from "../api";
import { useAppStore } from "../store";

export default function Home() {
  const navigate = useNavigate();
  const { setActiveVideoId, showToast } = useAppStore();

  const [mode, setMode] = useState<"upload" | "url">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(false);
  const [uploadPercent, setUploadPercent] = useState<number | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const dropped = e.dataTransfer.files[0];
    if (dropped && dropped.type.startsWith("video/")) {
      setFile(dropped);
      setMode("upload");
    }
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!confirmed) {
      showToast("Please confirm you own the rights to this video.", "error");
      return;
    }

    setLoading(true);
    try {
      let result;
      if (mode === "upload" && file) {
        result = await uploadVideo(file, confirmed, setUploadPercent);
      } else if (mode === "url" && url) {
        result = await submitVideoUrl(url, confirmed);
      } else {
        showToast("Please select a video file or enter a URL.", "error");
        return;
      }

      setActiveVideoId(result.id);
      showToast("Processing started!", "success");
      navigate(`/processing/${result.id}`);
    } catch (err: any) {
      showToast(err?.response?.data?.detail || "Something went wrong.", "error");
    } finally {
      setLoading(false);
      setUploadPercent(null);
    }
  };

  return (
    <div className="min-h-screen flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-lg">
        {/* Header */}
        <div className="text-center mb-10">
          <h1 className="text-4xl font-bold text-white mb-2">
            🎬 Snipster
          </h1>
          <p className="text-gray-400 text-lg">
            Turn one long video into multiple Shorts & Reels — in minutes.
          </p>
        </div>

        {/* Mode Toggle */}
        <div className="flex rounded-lg bg-gray-800 p-1 mb-6">
          <button
            onClick={() => setMode("upload")}
            className={`flex-1 py-2 rounded-md text-sm font-medium flex items-center justify-center gap-2 transition-colors ${
              mode === "upload"
                ? "bg-red-600 text-white"
                : "text-gray-400 hover:text-white"
            }`}
          >
            <Upload size={16} /> Upload File
          </button>
          <button
            onClick={() => setMode("url")}
            className={`flex-1 py-2 rounded-md text-sm font-medium flex items-center justify-center gap-2 transition-colors ${
              mode === "url"
                ? "bg-red-600 text-white"
                : "text-gray-400 hover:text-white"
            }`}
          >
            <Link size={16} /> YouTube URL
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-5">
          {/* Upload Drop Zone */}
          {mode === "upload" && (
            <div
              onDrop={handleDrop}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              className={`border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition-colors ${
                dragOver
                  ? "border-red-500 bg-red-950/20"
                  : file
                  ? "border-green-500 bg-green-950/20"
                  : "border-gray-600 hover:border-gray-500"
              }`}
              onClick={() => document.getElementById("file-input")?.click()}
            >
              <input
                id="file-input"
                type="file"
                accept="video/*"
                className="hidden"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
              {file ? (
                <div>
                  <p className="text-green-400 font-medium">✓ {file.name}</p>
                  <p className="text-gray-500 text-sm mt-1">
                    {(file.size / 1024 / 1024).toFixed(1)} MB
                  </p>
                </div>
              ) : (
                <div>
                  <Upload className="mx-auto text-gray-500 mb-3" size={36} />
                  <p className="text-gray-300 font-medium">
                    Drop your video here or click to browse
                  </p>
                  <p className="text-gray-500 text-sm mt-1">MP4, MOV, AVI, WebM</p>
                </div>
              )}
            </div>
          )}

          {/* YouTube URL Input */}
          {mode === "url" && (
            <div>
              <input
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://www.youtube.com/watch?v=..."
                className="w-full bg-gray-800 border border-gray-600 rounded-lg px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-red-500"
              />
            </div>
          )}

          {/* Ownership Confirmation */}
          <label className="flex items-start gap-3 cursor-pointer group">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
              className="mt-1 accent-red-500"
            />
            <div className="flex items-start gap-2">
              <ShieldCheck size={18} className="text-gray-400 mt-0.5 shrink-0" />
              <span className="text-sm text-gray-400 group-hover:text-gray-300">
                I confirm that I own the rights to this video and am authorized
                to repurpose and publish it.
              </span>
            </div>
          </label>

          {/* Submit */}
          <button
            type="submit"
            disabled={loading || !confirmed}
            className="w-full bg-red-600 hover:bg-red-500 disabled:bg-gray-700 disabled:cursor-not-allowed text-white font-semibold py-3 rounded-lg transition-colors"
          >
            {loading
              ? uploadPercent !== null
                ? `Uploading ${uploadPercent}%`
                : "Uploading…"
              : "Process Video →"}
          </button>
        </form>
      </div>
    </div>
  );
}
