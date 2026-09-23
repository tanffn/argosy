"use client";

import { useEffect, useState } from "react";

/** Read-only refresh on mount, focus and each minute; never retain stale success
 * after a failed refresh. The server remains the owner of financial freshness. */
export function useHomeRead<T>(load: (userId: string, signal: AbortSignal) => Promise<T>, userId: string) {
  const [result, setResult] = useState<{ userId: string; data: T | null; failed: boolean; reason?: string } | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let disposed = false;
    let controller: AbortController | null = null;
    const refresh = async () => {
      if (controller) return;
      const current = new AbortController();
      controller = current;
      const timeout = setTimeout(() => current.abort(), 20_000);
      try {
        const data = await load(userId, current.signal);
        if (!disposed) setResult({ userId, data, failed: false });
      } catch (error) {
        const reason = current.signal.aborted
          ? "The server did not respond within 20 seconds."
          : error instanceof TypeError
            ? "The browser could not connect to Argosy’s API."
            : "Argosy’s API could not return a usable response.";
        if (!disposed) setResult({ userId, data: null, failed: true, reason });
      } finally {
        clearTimeout(timeout);
        controller = null;
      }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 60_000);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      disposed = true;
      controller?.abort();
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [load, userId, attempt]);
  return {
    data: result?.userId === userId ? result.data : null,
    failed: result?.userId === userId && result.failed,
    reason: result?.userId === userId ? result.reason : undefined,
    retry: () => setAttempt((value) => value + 1),
  };
}
