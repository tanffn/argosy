"use client";

/**
 * Browser notification helper (Wave 4).
 *
 * Wraps the Web Notifications API with a no-op fallback when the API
 * is unavailable (Safari without permission flow, ancient browsers, or
 * permission denied/default). The in-app banner remains the always-on
 * surface — these notifications are an opt-in escalation when the user
 * has navigated away from the tab.
 */

type Permission = "granted" | "denied" | "default";

function isSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export function permission(): Permission {
  if (!isSupported()) return "denied";
  return Notification.permission;
}

export async function ensureNotificationPermission(): Promise<Permission> {
  if (!isSupported()) return "denied";
  if (Notification.permission === "granted") return "granted";
  if (Notification.permission === "denied") return "denied";
  try {
    const result = await Notification.requestPermission();
    return result;
  } catch {
    return "denied";
  }
}

export function notify(title: string, body: string, opts?: NotificationOptions): void {
  if (!isSupported()) return;
  if (Notification.permission !== "granted") return;
  try {
    new Notification(title, { body, ...opts });
  } catch {
    // Silently swallow — falling back to the in-app banner is the contract.
  }
}
