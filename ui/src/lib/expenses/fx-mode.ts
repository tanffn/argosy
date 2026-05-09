"use client";

import { useEffect, useState } from "react";

export type FxMode = "per_currency" | "nis";

const STORAGE_KEY = "argosy.expenses.fxMode";

export function useFxMode(): [FxMode, (m: FxMode) => void] {
  const [mode, setMode] = useState<FxMode>("per_currency");
  useEffect(() => {
    if (typeof window === "undefined") return;
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "per_currency" || stored === "nis") setMode(stored);
  }, []);
  const update = (m: FxMode) => {
    setMode(m);
    if (typeof window !== "undefined") {
      localStorage.setItem(STORAGE_KEY, m);
    }
  };
  return [mode, update];
}
