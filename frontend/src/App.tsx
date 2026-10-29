import { BrowserRouter, Routes, Route, NavLink, useLocation } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import { Clapperboard, Settings as SettingsIcon, Home as HomeIcon, LogOut, Loader2 } from "lucide-react";
import Home from "./pages/Home";
import Login from "./pages/Login";
import Processing from "./pages/Processing";
import Review from "./pages/Review";
import Settings from "./pages/Settings";
import { useAppStore } from "./store";
import { checkSession, getAuthConfig, getConnectedAccounts } from "./api";
import { clearToken, getToken, UNAUTHORIZED_EVENT } from "./api/client";

function Toast() {
  const { toast, clearToast } = useAppStore();
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(clearToast, 4000);
    return () => clearTimeout(t);
  }, [toast, clearToast]);

  if (!toast) return null;

  const colors = {
    success: "bg-green-800 border-green-600 text-green-100",
    error: "bg-red-900 border-red-700 text-red-100",
    info: "bg-gray-800 border-gray-600 text-gray-100",
  };

  return (
    <div
      className={`fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg border text-sm shadow-xl max-w-sm ${colors[toast.type]}`}
    >
      {toast.message}
    </div>
  );
}

function Navbar({ onLogout }: { onLogout?: () => void }) {
  const location = useLocation();
  // Hide nav on processing & review pages to keep focus
  const hide = ["/processing", "/review"].some((p) => location.pathname.startsWith(p));
  if (hide) return null;

  return (
    <nav className="fixed top-0 left-0 right-0 z-40 bg-gray-950/90 backdrop-blur border-b border-gray-800 flex items-center justify-between px-6 h-14">
      <NavLink to="/" className="flex items-center gap-2 text-white font-bold text-lg">
        <Clapperboard size={22} className="text-red-500" />
        Snipster
      </NavLink>
      <div className="flex items-center gap-1">
        <NavLink
          to="/"
          end
          className={({ isActive }) =>
            `flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm transition-colors ${
              isActive ? "bg-gray-800 text-white" : "text-gray-400 hover:text-white"
            }`
          }
        >
          <HomeIcon size={15} /> Home
        </NavLink>
        <NavLink
          to="/settings"
          className={({ isActive }) =>
            `flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm transition-colors ${
              isActive ? "bg-gray-800 text-white" : "text-gray-400 hover:text-white"
            }`
          }
        >
          <SettingsIcon size={15} /> Settings
        </NavLink>
        {onLogout && (
          <button
            onClick={onLogout}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-gray-400 hover:text-white transition-colors"
          >
            <LogOut size={15} /> Sign out
          </button>
        )}
      </div>
    </nav>
  );
}

function AppShell({ onLogout }: { onLogout?: () => void }) {
  const { setAccounts } = useAppStore();
  useEffect(() => {
    getConnectedAccounts().then(setAccounts).catch(() => {});
  }, [setAccounts]);

  return (
    <div className="min-h-screen bg-gray-950">
      <Navbar onLogout={onLogout} />
      <div className="pt-14">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/processing/:videoId" element={<Processing />} />
          <Route path="/review/:videoId" element={<Review />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </div>
      <Toast />
    </div>
  );
}

type GateState = "loading" | "login" | "ok" | "error" | "misconfigured";

/** Shows the login page when the API wants a password; otherwise the app. */
function AuthGate() {
  const [state, setState] = useState<GateState>("loading");
  const [required, setRequired] = useState(false);

  const check = useCallback(async () => {
    try {
      const cfg = await getAuthConfig();
      setRequired(cfg.auth_required);
      if (cfg.misconfigured) return setState("misconfigured");
      if (!cfg.auth_required) return setState("ok");
      if (!getToken()) return setState("login");
      try {
        await checkSession();
        setState("ok");
      } catch (err: any) {
        setState(err?.response?.status === 401 ? "login" : "error");
      }
    } catch {
      setState("error");
    }
  }, []);

  useEffect(() => {
    check();
  }, [check]);

  // Any request that comes back 401 later (token expired) sends us back to the login page.
  useEffect(() => {
    const onUnauthorized = () => setState("login");
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  if (state === "loading") {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <Loader2 className="animate-spin text-gray-400" size={40} />
      </div>
    );
  }
  if (state === "misconfigured") {
    return (
      <div className="min-h-screen bg-gray-950 flex flex-col items-center justify-center gap-3 text-center p-6">
        <p className="text-yellow-300">This deployment has no password set, so it is locked.</p>
        <p className="text-gray-500 text-sm max-w-md">
          Add <code className="text-gray-300">ADMIN_PASSWORD</code> and{" "}
          <code className="text-gray-300">SECRET_KEY</code> in the Vercel project settings, then redeploy.
        </p>
      </div>
    );
  }
  if (state === "error") {
    return (
      <div className="min-h-screen bg-gray-950 flex flex-col items-center justify-center gap-4 text-center p-6">
        <p className="text-red-400">Cannot reach the Snipster server.</p>
        <p className="text-gray-500 text-sm max-w-sm">Check your connection, then try again.</p>
        <button
          onClick={() => {
            setState("loading");
            check();
          }}
          className="bg-gray-800 hover:bg-gray-700 text-white text-sm px-5 py-2 rounded-lg"
        >
          Try again
        </button>
      </div>
    );
  }
  if (state === "login") return <Login onSuccess={() => setState("ok")} />;

  return (
    <AppShell
      onLogout={
        required
          ? () => {
              clearToken();
              setState("login");
            }
          : undefined
      }
    />
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthGate />
    </BrowserRouter>
  );
}
