import { create } from "zustand";
import type { Clip, ConnectedAccounts } from "../types";

interface AppState {
  // Active video being processed or reviewed
  activeVideoId: string | null;
  setActiveVideoId: (id: string | null) => void;

  // Clips for the active video
  clips: Clip[];
  setClips: (clips: Clip[]) => void;
  updateClip: (updated: Clip) => void;

  // Connected platform accounts
  accounts: ConnectedAccounts | null;
  setAccounts: (accounts: ConnectedAccounts) => void;

  // Toast notification (simple global)
  toast: { message: string; type: "success" | "error" | "info" } | null;
  showToast: (message: string, type?: "success" | "error" | "info") => void;
  clearToast: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  activeVideoId: null,
  setActiveVideoId: (id) => set({ activeVideoId: id }),

  clips: [],
  setClips: (clips) => set({ clips }),
  updateClip: (updated) =>
    set((state) => ({
      clips: state.clips.map((c) => (c.id === updated.id ? updated : c)),
    })),

  accounts: null,
  setAccounts: (accounts) => set({ accounts }),

  toast: null,
  showToast: (message, type = "info") => set({ toast: { message, type } }),
  clearToast: () => set({ toast: null }),
}));
