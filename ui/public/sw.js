/**
 * Argosy service worker — Spec E commit #7.
 *
 * Single responsibility: receive web-push events and render them via the
 * browser's native notification surface. Registered from
 * <PushSubscriptionCard> when the user opts in to push notifications.
 *
 * Why this file is intentionally tiny
 * ===================================
 *
 * v1 web push from the server sends UNENCRYPTED payloads via VAPID JWT only
 * (see argosy/services/web_push.py docstring §4). RFC 8291 AES128GCM is a
 * follow-on commit. That means two shapes are possible at runtime:
 *
 *   1. event.data is present  -> server sent a JSON payload (when v2 ships
 *      AES128GCM encryption); we parse it for title + body + deep_link.
 *   2. event.data is null     -> the v1 path; we render a static "Argosy
 *      notification" with a generic body, since the SW has no way to know
 *      the payload contents without server-side encryption.
 *
 * Either way the user sees SOMETHING — silent push notifications are
 * forbidden by Chrome under `userVisibleOnly: true` and would deregister
 * the subscription if we returned without calling showNotification().
 *
 * No caching, no offline shell, no fetch interception — those would be
 * larger architectural decisions out of Spec E's scope. If a future
 * commit adds PWA install + offline support, we extend this file then.
 */

/* eslint-env serviceworker */

const DEFAULT_TITLE = "Argosy notification";
const DEFAULT_BODY = "Open Argosy to review the latest activity.";
const DEFAULT_ICON = "/logo.png";
const DEFAULT_BADGE = "/logo.png";

/**
 * Parse the push event's data payload, falling back to a static message
 * when (a) there is no payload (v1 unencrypted path) or (b) the payload
 * is not valid JSON (unexpected vendor behaviour).
 */
function readPayload(event) {
  if (!event || !event.data) {
    return { title: DEFAULT_TITLE, body: DEFAULT_BODY, data: {} };
  }
  try {
    const parsed = event.data.json();
    return {
      title: typeof parsed.title === "string" ? parsed.title : DEFAULT_TITLE,
      body: typeof parsed.body === "string" ? parsed.body : DEFAULT_BODY,
      data: typeof parsed === "object" && parsed !== null ? parsed : {},
    };
  } catch (_jsonErr) {
    // Some vendors send plain text bodies; surface them as-is in the body
    // slot so the user gets the raw message rather than the default.
    let text;
    try {
      text = event.data.text();
    } catch (_textErr) {
      text = DEFAULT_BODY;
    }
    return {
      title: DEFAULT_TITLE,
      body: text || DEFAULT_BODY,
      data: { raw: text || null },
    };
  }
}

/**
 * Push event handler. Always calls showNotification() so the browser does
 * not flag this SW as silently-suppressing pushes (which would cause the
 * subscription to be revoked).
 */
self.addEventListener("push", (event) => {
  const { title, body, data } = readPayload(event);
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: DEFAULT_ICON,
      badge: DEFAULT_BADGE,
      data,
      // Each push is a fresh interruption — collapse same-tag pushes
      // server-side via the dispatch_ledger, NOT here.
      renotify: false,
    }),
  );
});

/**
 * Click handler — focus an existing tab when one is open, otherwise open
 * the deep-link the producer attached to the payload (falling back to the
 * app root).
 */
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const deepLink =
    typeof data.deep_link === "string" && data.deep_link.startsWith("/")
      ? data.deep_link
      : "/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          // Reuse the first same-origin tab if one is around.
          if ("focus" in client) {
            client.focus();
            if ("navigate" in client && deepLink) {
              try {
                client.navigate(deepLink);
              } catch (_navErr) {
                // navigate() is best-effort; same-origin policy + browser
                // version differences may reject it. The focus() above
                // still surfaces the existing tab.
              }
            }
            return undefined;
          }
        }
        // No tab open — pop a new one at the deep link.
        return self.clients.openWindow(deepLink);
      }),
  );
});

/**
 * Lifecycle bookkeeping. The skipWaiting / clients.claim pair ensures the
 * new SW takes over immediately on update instead of waiting for every
 * tab to close, so a re-deploy doesn't strand users on a stale SW that
 * may have been built against an older payload contract.
 */
self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
