import { useState } from "react";
import { Clapperboard, Loader2 } from "lucide-react";
import { login } from "../api";
import { setToken } from "../api/client";

export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      setToken(await login(password));
      onSuccess();
    } catch (err: any) {
      const status = err?.response?.status;
      setError(
        status === 429
          ? "Too many failed attempts. Try again in 15 minutes."
          : status === 401
          ? "Wrong password."
          : "Could not reach the server."
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-6 bg-gray-950">
      <form onSubmit={submit} className="w-full max-w-sm space-y-5">
        <div className="text-center">
          <Clapperboard size={36} className="mx-auto text-red-500 mb-3" />
          <h1 className="text-2xl font-bold text-white">Snipster</h1>
          <p className="text-gray-500 text-sm mt-1">Sign in to continue</p>
        </div>

        <input
          type="password"
          autoFocus
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          className="w-full bg-gray-800 border border-gray-600 rounded-lg px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-red-500"
        />

        {error && <p className="text-sm text-red-400">{error}</p>}

        <button
          type="submit"
          disabled={loading || !password}
          className="w-full flex items-center justify-center gap-2 bg-red-600 hover:bg-red-500 disabled:bg-gray-700 disabled:cursor-not-allowed text-white font-semibold py-3 rounded-lg transition-colors"
        >
          {loading && <Loader2 size={16} className="animate-spin" />}
          Sign in
        </button>
      </form>
    </div>
  );
}
