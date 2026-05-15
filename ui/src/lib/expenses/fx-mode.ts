"use client";

import { useSyncExternalStore } from "react";

export type FxMode = "per_currency" | "nis";

const STORAGE_KEY = "argosy.expenses.fxMode";
const CHANGE_EVENT = "argosy:fxmode-changed";

// Cross-component sync goal: the FxToggle in the layout writes localStorage
// + dispatches a CustomEvent; every other useFxMode() consumer in the same
// tab picks it up via that event, and other tabs pick it up via the native
// `storage` event.
//
// SSR contract: must return the same value during the server render and
// the client's first render (before localStorage is read), otherwise React
// emits a hydration mismatch. `useSyncExternalStore` is the canonical fix —
// the third argument is the server snapshot, and React waits to switch to
// the client snapshot until *after* hydration. We don't pay any flicker
// because React rewinds and re-renders in a separate commit.

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(CHANGE_EVENT, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(CHANGE_EVENT, callback);
    window.removeEventListener("storage", callback);
  };
}

function getClientSnapshot(): FxMode {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === "nis" ? "nis" : "per_currency";
}

function getServerSnapshot(): FxMode {
  return "per_currency";
}

export function useFxMode(): [FxMode, (m: FxMode) => void] {
  const mode = useSyncExternalStore(
    subscribe, getClientSnapshot, getServerSnapshot,
  );
  const update = (m: FxMode) => {
    if (typeof window === "undefined") return;
    localStorage.setItem(STORAGE_KEY, m);
    window.dispatchEvent(new Event(CHANGE_EVENT));
  };
  return [mode, update];
}
