import axios from "axios";

// Always relative: on Vercel the UI and the API are served from the same origin,
// and in local dev the Vite proxy forwards /api to the backend (see vite.config.ts).
const api = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

const TOKEN_KEY = "snipster_token";

export const getToken = (): string | null => localStorage.getItem(TOKEN_KEY);
export const setToken = (token: string) => localStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

/** Fired when the API says the login is missing or expired, so the app can show the login page. */
export const UNAUTHORIZED_EVENT = "snipster:unauthorized";

api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use(
  (res) => res,
  (err) => {
    // A wrong password on the login form is also a 401 — that page handles its own error.
    const isLogin = err?.config?.url === "/auth/session" && err?.config?.method === "post";
    if (err?.response?.status === 401 && !isLogin) {
      clearToken();
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    return Promise.reject(err);
  }
);

export default api;
