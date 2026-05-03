"use client";

import { useEffect, useRef, useState } from "react";

/**
 * WebSocket subscription hook for /ws.
 *
 * Returns the most recent payload for any of `events` (filtered by
 * top-level `event` field). The Home page uses this to refresh on
 * `daily_brief.ready` + `agent.run.finished` events.
 */
export interface WSEvent<T = unknown> {
  event: string;
  payload: T;
}

export function useWSEvents<T = unknown>(events: string[]): WSEvent<T> | null {
  const [last, setLast] = useState<WSEvent<T> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let cancelled = false;
    const url = `${window.location.protocol === "https:" ? "wss" : "ws"}://${
      window.location.host
    }/ws`;
    let ws: WebSocket | null = null;
    try {
      ws = new WebSocket(url);
    } catch {
      return;
    }
    wsRef.current = ws;
    ws.onmessage = (ev: MessageEvent<string>) => {
      if (cancelled) return;
      try {
        const parsed = JSON.parse(ev.data) as WSEvent<T>;
        if (parsed && events.includes(parsed.event)) {
          setLast(parsed);
        }
      } catch {
        // non-JSON messages (e.g. "connected") are ignored
      }
    };
    return () => {
      cancelled = true;
      try {
        ws?.close();
      } catch {
        // ignore
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events.join("|")]);

  return last;
}
