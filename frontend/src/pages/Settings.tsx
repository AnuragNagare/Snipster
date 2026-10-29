import { useEffect, useState } from "react";
import { Play, Unlink, ExternalLink } from "lucide-react";
import {
  getConnectedAccounts,
  startGoogleLogin,
  disconnectYoutube,
} from "../api";
import { useAppStore } from "../store";
import type { ConnectedAccounts } from "../types";

export default function Settings() {
  const { showToast } = useAppStore();
  const [accounts, setAccounts] = useState<ConnectedAccounts | null>(null);
  const [, setLoading] = useState(true);

  useEffect(() => {
    getConnectedAccounts()
      .then(setAccounts)
      .catch(() => showToast("Failed to load account info.", "error"))
      .finally(() => setLoading(false));
  }, [showToast]);

  const connect = async (start: () => Promise<string>) => {
    try {
      window.location.href = await start();
    } catch {
      showToast("Could not start sign-in.", "error");
    }
  };

  const handleDisconnectYT = async () => {
    await disconnectYoutube();
    setAccounts((a) => a ? { ...a, youtube_connected: false, youtube_channel_name: undefined } : a);
    showToast("YouTube disconnected.", "info");
  };

  return (
    <div className="min-h-screen p-6 max-w-2xl mx-auto">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-white">Settings</h1>
        <p className="text-gray-400 mt-1">Connect your YouTube channel.</p>
      </div>

      <div className="space-y-4">
        {/* YouTube */}
        <div className="bg-gray-900 border border-gray-700 rounded-xl p-5 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className="w-10 h-10 bg-red-600 rounded-full flex items-center justify-center">
              <Play size={20} className="text-white" />
            </div>
            <div>
              <p className="text-white font-medium">YouTube</p>
              {accounts?.youtube_connected ? (
                <p className="text-green-400 text-sm">
                  ✓ {accounts.youtube_channel_name || "Connected"}
                </p>
              ) : (
                <p className="text-gray-500 text-sm">Not connected</p>
              )}
            </div>
          </div>

          {accounts?.youtube_connected ? (
            <button
              onClick={handleDisconnectYT}
              className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-red-400 transition-colors"
            >
              <Unlink size={14} /> Disconnect
            </button>
          ) : (
            <button
              onClick={() => connect(startGoogleLogin)}
              className="bg-red-600 hover:bg-red-500 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors flex items-center gap-1.5"
            >
              <ExternalLink size={14} /> Connect
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
