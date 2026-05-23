"use client";

import { useEffect, useRef, useState } from "react";

/**
 * WebSocket subscription hook for /ws.
 *
 * Returns the most recent payload for any of `events` (filtered by
 * top-level `event` field). The Home page uses this to refresh on
 * `daily_brief.ready` + `agent.run.finished` events.
 *
 * `onEvent` is an opt-in escape hatch: when provided, it is called
 * synchronously from the WS `onmessage` handler for every matching event
 * BEFORE the React setState call. This bypasses React batching entirely
 * and guarantees no events are dropped even if two arrive in the same
 * tick. Existing callers that omit `onEvent` are unaffected.
 */
export interface WSEvent<T = unknown> {
  event: string;
  payload: T;
}

export interface UseWSEventsOpts<T> {
  onEvent?: (e: WSEvent<T>) => void;
}

export function useWSEvents<T = unknown>(
  events: string[],
  opts?: UseWSEventsOpts<T>,
): WSEvent<T> | null {
  const [last, setLast] = useState<WSEvent<T> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  // Keep a stable ref to opts.onEvent so the effect closure doesn't
  // need to be torn down on every render.
  const onEventRef = useRef<UseWSEventsOpts<T>["onEvent"]>(opts?.onEvent);
  useEffect(() => {
    onEventRef.current = opts?.onEvent;
  });

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
          // Fire the synchronous callback first (bypasses React batching).
          onEventRef.current?.(parsed);
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
