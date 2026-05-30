/**
 * PushSubscriptionCard — Spec E commit #7 / spec §6.2.
 *
 * Opt-in card for web push notifications. Sits at the top of
 * /settings/notifications. Responsibilities:
 *
 *   1. Detect browser support (serviceWorker + PushManager + Notification).
 *   2. Fetch the server's VAPID public key on mount.
 *   3. Register /sw.js when the user clicks "Enable notifications".
 *   4. Call pushManager.subscribe({ userVisibleOnly: true, applicationServerKey })
 *      with the URL-safe base64-decoded VAPID key.
 *   5. POST the resulting PushSubscription to /api/notifications/subscribe.
 *   6. Reflect state ("Subscribed" / "Not subscribed" / "Unsupported").
 *   7. Allow unsubscribe + "send test notification" round-trips.
 *
 * What this file is NOT:
 *   - Not where notifications are rendered. The browser SW does that
 *     (see ui/public/sw.js); we only own the subscription lifecycle UI.
 *   - Not where the preference matrix lives. That's a sibling component
 *     on the same page (NotificationPreferenceMatrix below in
 *     ui/src/app/settings/notifications/page.tsx).
 */
"use client";

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type NotificationSubscriptionDTO,
  type NotificationTestPushResponse,
} from "@/lib/api";

interface PushSubscriptionCardProps {
  userId: string;
}

type SupportState =
  | { kind: "checking" }
  | { kind: "supported" }
  | { kind: "unsupported"; reason: string };

type VapidState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; publicKey: string }
  | { kind: "unconfigured"; hint: string }
  | { kind: "error"; message: string };

type SubscriptionState =
  | { kind: "loading" }
  | { kind: "active"; row: NotificationSubscriptionDTO }
  | { kind: "none" }
  | { kind: "error"; message: string };

/**
 * Decode a URL-safe base64 VAPID public key into the Uint8Array expected
 * by pushManager.subscribe(). Mirrors the snippet in the WebPush spec:
 * https://developer.mozilla.org/en-US/docs/Web/API/Push_API/Best_practices
 */
function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  const output = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i += 1) {
    output[i] = rawData.charCodeAt(i);
  }
  return output;
}

/**
 * Convert a browser PushSubscription's `keys` (an ArrayBuffer dict) into
 * URL-safe base64 strings for the subscribe POST.
 */
function arrayBufferToBase64Url(buffer: ArrayBuffer | null): string | null {
  if (!buffer) return null;
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function checkBrowserSupport(): SupportState {
  if (typeof window === "undefined") {
    return { kind: "checking" };
  }
  if (!("serviceWorker" in navigator)) {
    return { kind: "unsupported", reason: "Service workers are not available in this browser." };
  }
  if (!("PushManager" in window)) {
    return { kind: "unsupported", reason: "Push messaging is not supported in this browser." };
  }
  if (!("Notification" in window)) {
    return { kind: "unsupported", reason: "Notification API is not available in this browser." };
  }
  // Service workers require a secure context (https or localhost).
  // Chrome lies and returns true for window.isSecureContext on file://
  // pages, so cross-check the protocol explicitly.
  if (
    location.protocol !== "https:" &&
    location.hostname !== "localhost" &&
    location.hostname !== "127.0.0.1"
  ) {
    return {
      kind: "unsupported",
      reason: "Push notifications require HTTPS (or localhost for dev).",
    };
  }
  return { kind: "supported" };
}

export function PushSubscriptionCard({ userId }: PushSubscriptionCardProps) {
  const [support, setSupport] = useState<SupportState>({ kind: "checking" });
  const [vapid, setVapid] = useState<VapidState>({ kind: "idle" });
  const [subscription, setSubscription] = useState<SubscriptionState>({
    kind: "loading",
  });
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [testResult, setTestResult] =
    useState<NotificationTestPushResponse | null>(null);

  // Step 1 — browser-support detection runs once on mount. The check
  // touches `window` / `navigator`, so it cannot run during SSR;
  // useEffect on mount is the safe place to populate state from those
  // browser globals.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-shot browser-globals probe; cannot run during SSR
    setSupport(checkBrowserSupport());
  }, []);

  // Step 2 — fetch VAPID key + the current subscription row in parallel
  // once we know the browser can handle push at all.
  const loadServerState = useCallback(async () => {
    if (support.kind !== "supported") return;
    setVapid({ kind: "loading" });
    setSubscription({ kind: "loading" });

    try {
      const vapidRes = await api.notifications.vapidKey();
      setVapid({ kind: "ready", publicKey: vapidRes.public_key });
    } catch (err) {
      // 503 = vapid_not_configured per the route handler. The fetch
      // helper throws an Error("HTTP 503 ...") so we parse it.
      const message = String(err);
      if (message.includes("HTTP 503")) {
        setVapid({
          kind: "unconfigured",
          hint: "VAPID keys are not configured on this server. Push notifications are disabled until ~/.argosy/vapid_creds.json is seeded.",
        });
      } else {
        setVapid({ kind: "error", message });
      }
    }

    // Cross-reference any locally-known PushSubscription with the
    // server-side list. The server is the source of truth for "did we
    // receive the subscribe POST?"; the browser endpoint is just our
    // key.
    let browserEndpoint: string | null = null;
    try {
      const reg = await navigator.serviceWorker.getRegistration("/sw.js");
      if (reg) {
        const sub = await reg.pushManager.getSubscription();
        if (sub) browserEndpoint = sub.endpoint;
      }
    } catch {
      // Ignore — fall back to "no local subscription"; the server list
      // below will still tell us if anything is registered.
    }

    try {
      const rows = await api.notifications.listSubscriptions(userId);
      const match = browserEndpoint
        ? rows.find((r) => r.endpoint === browserEndpoint && r.status === "active")
        : rows.find((r) => r.status === "active");
      if (match) {
        setSubscription({ kind: "active", row: match });
      } else {
        setSubscription({ kind: "none" });
      }
    } catch (err) {
      setSubscription({ kind: "error", message: String(err) });
    }
  }, [support.kind, userId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount; loadServerState() sets local state from the API
    loadServerState();
  }, [loadServerState]);

  const handleSubscribe = async () => {
    if (vapid.kind !== "ready") {
      setActionError("VAPID key is not ready; cannot subscribe yet.");
      return;
    }
    setBusy(true);
    setActionError(null);
    setTestResult(null);
    try {
      // 1. Register the SW (no-op if already registered for the same URL).
      const reg = await navigator.serviceWorker.register("/sw.js");
      // 2. Wait until the SW is actually controlling pages — register()
      // resolves before activation in some browsers.
      await navigator.serviceWorker.ready;
      // 3. Ask the user for permission. If they already granted it the
      // call is a no-op.
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        throw new Error("Notification permission denied.");
      }
      // 4. Subscribe via the push manager.
      // The pushManager.subscribe applicationServerKey field accepts a
      // BufferSource; our urlBase64ToUint8Array returns a generic
      // Uint8Array<ArrayBufferLike> which TS rejects until we pin the
      // backing buffer to ArrayBuffer.
      const keyBytes = urlBase64ToUint8Array(vapid.publicKey);
      const applicationServerKey = keyBytes.buffer.slice(
        keyBytes.byteOffset,
        keyBytes.byteOffset + keyBytes.byteLength,
      ) as ArrayBuffer;
      const pushSub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey,
      });
      const json = pushSub.toJSON();
      const p256dh =
        (json.keys && json.keys.p256dh) ||
        arrayBufferToBase64Url(pushSub.getKey("p256dh"));
      const auth =
        (json.keys && json.keys.auth) ||
        arrayBufferToBase64Url(pushSub.getKey("auth"));
      // 5. Tell the backend.
      const row = await api.notifications.subscribe(userId, {
        channel: "web_push",
        endpoint: pushSub.endpoint,
        p256dh,
        auth,
      });
      setSubscription({ kind: "active", row });
    } catch (err) {
      setActionError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleUnsubscribe = async () => {
    if (subscription.kind !== "active") return;
    setBusy(true);
    setActionError(null);
    setTestResult(null);
    try {
      // Browser-side opt-out first (so a server failure doesn't leave
      // the browser still pushing events nowhere).
      try {
        const reg = await navigator.serviceWorker.getRegistration("/sw.js");
        if (reg) {
          const sub = await reg.pushManager.getSubscription();
          if (sub) await sub.unsubscribe();
        }
      } catch {
        // best-effort; continue with server delete
      }
      await api.notifications.unsubscribe(userId, subscription.row.id);
      setSubscription({ kind: "none" });
    } catch (err) {
      setActionError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleTestPush = async () => {
    setBusy(true);
    setActionError(null);
    setTestResult(null);
    try {
      const res = await api.notifications.testPush(userId);
      setTestResult(res);
    } catch (err) {
      setActionError(String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Web push notifications</CardTitle>
        <CardDescription>
          Opt this browser in to receive Argosy alerts when the tab is
          backgrounded or closed. The system uses VAPID — only the keys
          held by your local server can send to your subscription.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {support.kind === "checking" && (
          <p className="text-sm text-muted-foreground">Detecting browser support…</p>
        )}
        {support.kind === "unsupported" && (
          <p className="text-sm text-error">{support.reason}</p>
        )}
        {support.kind === "supported" && (
          <>
            {vapid.kind === "loading" && (
              <p className="text-sm text-muted-foreground">
                Loading VAPID key from server…
              </p>
            )}
            {vapid.kind === "unconfigured" && (
              <p className="text-sm text-warning">{vapid.hint}</p>
            )}
            {vapid.kind === "error" && (
              <p className="text-sm text-error font-mono">{vapid.message}</p>
            )}

            {subscription.kind === "loading" && (
              <p className="text-sm text-muted-foreground">
                Reading current subscription state…
              </p>
            )}
            {subscription.kind === "error" && (
              <p className="text-sm text-error font-mono">{subscription.message}</p>
            )}
            {subscription.kind === "active" && (
              <div className="flex flex-col gap-2 text-sm">
                <p>
                  <span className="font-semibold text-success">Subscribed.</span>{" "}
                  This browser will receive push notifications.
                </p>
                <p className="text-xs text-muted-foreground font-mono break-all">
                  endpoint: {subscription.row.endpoint.slice(0, 80)}…
                </p>
              </div>
            )}
            {subscription.kind === "none" && (
              <p className="text-sm text-muted-foreground">
                This browser is not subscribed. Push notifications are off.
              </p>
            )}

            <div className="flex flex-wrap items-center gap-2 pt-2">
              {subscription.kind === "none" && (
                <Button
                  onClick={handleSubscribe}
                  size="sm"
                  disabled={busy || vapid.kind !== "ready"}
                >
                  Enable notifications
                </Button>
              )}
              {subscription.kind === "active" && (
                <>
                  <Button
                    onClick={handleUnsubscribe}
                    size="sm"
                    variant="outline"
                    disabled={busy}
                  >
                    Unsubscribe this browser
                  </Button>
                  <Button
                    onClick={handleTestPush}
                    size="sm"
                    variant="outline"
                    disabled={busy}
                  >
                    Send test notification
                  </Button>
                </>
              )}
              <Button
                onClick={loadServerState}
                size="sm"
                variant="ghost"
                disabled={busy}
              >
                Refresh
              </Button>
            </div>

            {actionError && (
              <p className="text-sm text-error font-mono">{actionError}</p>
            )}
            {testResult && (
              <div className="text-xs text-muted-foreground font-mono break-all">
                Test dispatched. notification_id={testResult.notification_id};{" "}
                sent={JSON.stringify(testResult.channels_sent)}; skipped=
                {JSON.stringify(testResult.channels_skipped)}
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default PushSubscriptionCard;
