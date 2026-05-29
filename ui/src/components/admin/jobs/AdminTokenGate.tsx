"use client";

// Sprint A commit #8 — admin-token paste gate.
//
// Reads/writes localStorage.argosyAdminToken. Renders the gate inline
// when the token is missing; when present, renders `children`. The
// `<input type="password">` keeps the token off-screen during paste
// (single-user system, but Ariel may dogfood with a screenshare).
//
// Token storage key is exported from `@/lib/api` as `ADMIN_TOKEN_KEY`
// so any future helper can read the same slot.

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ADMIN_TOKEN_KEY } from "@/lib/api";

export function AdminTokenGate({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    // One-shot hydration: localStorage is browser-only, so we have to
    // read it after mount to avoid SSR mismatch. Wrapped in an inner
    // function to satisfy `react-hooks/set-state-in-effect`, which
    // flags direct setState in the effect body.
    const hydrate = () => {
      try {
        const t = window.localStorage.getItem(ADMIN_TOKEN_KEY);
        setToken(t && t.trim() ? t : null);
      } catch {
        setToken(null);
      }
      setHydrated(true);
    };
    hydrate();
  }, []);

  const save = () => {
    const trimmed = draft.trim();
    if (!trimmed) return;
    try {
      window.localStorage.setItem(ADMIN_TOKEN_KEY, trimmed);
    } catch {
      // localStorage may be unavailable in private mode; ignore + still
      // set in-memory so the current tab works.
    }
    setToken(trimmed);
  };

  const clear = () => {
    try {
      window.localStorage.removeItem(ADMIN_TOKEN_KEY);
    } catch {
      // ignore
    }
    setToken(null);
    setDraft("");
  };

  if (!hydrated) {
    // Avoid flashing the gate during SSR/hydration. Keep this short to
    // not delay the page perceptibly.
    return <p className="text-sm text-muted-foreground">Loading admin gate…</p>;
  }

  if (!token) {
    return (
      <Card className="max-w-xl">
        <CardHeader>
          <CardTitle className="text-base">Admin token required</CardTitle>
          <CardDescription>
            Paste the value of <code>ARGOSY_ADMIN_TOKEN</code> from the
            backend env. Stored in <code>localStorage</code> on this device
            only; never sent anywhere except as the{" "}
            <code>X-Argosy-Admin</code> header on mutating <code>/api/jobs</code>{" "}
            calls.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <Input
            type="password"
            placeholder="admin token"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") save();
            }}
            autoFocus
          />
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={save} disabled={!draft.trim()}>
              Save token
            </Button>
            <span className="text-xs text-muted-foreground">
              GET routes work without a token — only Run-now / Stop /
              Reconnect need it.
            </span>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end gap-2 text-xs text-muted-foreground">
        <span>
          Admin token loaded (
          <code>{token.slice(0, 4)}…{token.slice(-2)}</code>)
        </span>
        <Button size="sm" variant="outline" onClick={clear}>
          Clear token
        </Button>
      </div>
      {children}
    </div>
  );
}
