"use client";

import { useEffect, useState } from "react";

export type FxMode = "per_currency" | "nis";

const STORAGE_KEY = "argosy.expenses.fxMode";
const CHANGE_EVENT = "argosy:fxmode-changed";

// All useFxMode() consumers stay in sync through a same-tab CustomEvent +
// the cross-tab `storage` event. Without this, the FxToggle in the layout
// would update its own React state + write localStorage, but every other
// component that called useFxMode() (TransactionsTable etc.) would keep
// showing whatever value it read on its initial mount.

function readStored(): FxMode {
  if (typeof window === "undefined") return "per_currency";
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === "nis" ? "nis" : "per_currency";
}

export function useFxMode(): [FxMode, (m: FxMode) => void] {
  const [mode, setMode] = useState<FxMode>(() => readStored());

  useEffect(() => {
    if (typeof window === "undefined") return;
    const sync = () => setMode(readStored());
    window.addEventListener(CHANGE_EVENT, sync);
    window.addEventListener("storage", sync); // cross-tab
    return () => {
      window.removeEventListener(CHANGE_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  const update = (m: FxMode) => {
    setMode(m);
    if (typeof window !== "undefined") {
      localStorage.setItem(STORAGE_KEY, m);
      window.dispatchEvent(new Event(CHANGE_EVENT));
    }
  };
  return [mode, update];
}
