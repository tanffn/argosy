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
 *
 * Resilience: when the connection drops (backend restart, network blip,
 * server crash), the hook reconnects with exponential backoff (1s → 2s
 * → 4s → … capped at 30s). Backoff resets to 1s on a successful open.
 * No explicit "disconnected" state is exposed; events resume arriving
 * once a reconnect succeeds.
 */
export interface WSEvent<T = unknown> {
  event: string;
  payload: T;
}

export interface UseWSEventsOpts<T> {
  onEvent?: (e: WSEvent<T>) => void;
}

// Derive the WebSocket URL from the same env var that REST uses
// (`NEXT_PUBLIC_API_URL`, default `http://localhost:8000`). The prior
// implementation used `window.location.host`, which points at the
// Next.js dev server (`:1337`) — Next.js does not proxy /ws upgrades, so
// the connection silently timed out and every WS-driven UI (cascade
// panel, daily-brief flash, proposals live updates) saw zero events.
function resolveWsUrl(): string {
  const apiBase =
    typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_URL
      ? process.env.NEXT_PUBLIC_API_URL
      : "http://localhost:8000";
  // http://host → ws://host, https://host → wss://host
  const wsBase = apiBase.replace(/^http(s?):/, "ws$1:");
  return `${wsBase}/ws`;
}

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30_000;

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
    let attempt = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    const url = resolveWsUrl();

    function scheduleReconnect() {
      if (cancelled) return;
      const delay = Math.min(
        RECONNECT_BASE_MS * 2 ** attempt,
        RECONNECT_MAX_MS,
      );
      attempt += 1;
      reconnectTimer = setTimeout(connect, delay);
    }

    function connect() {
      reconnectTimer = null;
      if (cancelled) return;
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;
      ws.onopen = () => {
        if (cancelled) {
          try {
            ws.close();
          } catch {
            /* ignore */
          }
          return;
        }
        // Successful open: reset backoff so the next disconnect retries
        // promptly rather than waiting out the prior accumulated delay.
        attempt = 0;
      };
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
      ws.onclose = () => {
        if (cancelled) return;
        scheduleReconnect();
      };
      ws.onerror = () => {
        // `onerror` fires before `onclose` on connection failure;
        // `onclose` will schedule the reconnect. Suppressing the default
        // here keeps the console quiet during expected backoff cycles.
      };
    }

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      try {
        wsRef.current?.close();
      } catch {
        /* ignore */
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events.join("|")]);

  return last;
}
