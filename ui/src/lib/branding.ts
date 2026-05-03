// Argosy Phase 6: per-tenant branding fetcher.
//
// `useBranding` (hook) and `fetchBranding` (server) both call
// `GET /api/branding?user_id=...` and cache the result in-memory for
// the session.

import { useEffect, useState } from "react";

export type Branding = {
  app_name: string;
  theme: {
    primary: string;
    accent: string;
  };
  logo_url: string;
  favicon_url: string;
  support_email: string;
};

const DEFAULT_BRANDING: Branding = {
  app_name: "Argosy",
  theme: { primary: "#0ea5e9", accent: "#f59e0b" },
  logo_url: "/logo.svg",
  favicon_url: "/favicon.ico",
  support_email: "support@argosy.app",
};

const CACHE = new Map<string, Branding>();

const apiBase = (): string =>
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function fetchBranding(userId: string): Promise<Branding> {
  const cached = CACHE.get(userId);
  if (cached) return cached;
  try {
    const r = await fetch(`${apiBase()}/api/branding?user_id=${encodeURIComponent(userId)}`, {
      cache: "no-store",
    });
    if (!r.ok) return DEFAULT_BRANDING;
    const body = (await r.json()) as Branding;
    CACHE.set(userId, body);
    return body;
  } catch {
    return DEFAULT_BRANDING;
  }
}

export function useBranding(userId: string | null): Branding {
  const [branding, setBranding] = useState<Branding>(DEFAULT_BRANDING);
  useEffect(() => {
    if (!userId) return;
    let alive = true;
    fetchBranding(userId).then((b) => {
      if (alive) setBranding(b);
    });
    return () => {
      alive = false;
    };
  }, [userId]);
  return branding;
}

export { DEFAULT_BRANDING };
