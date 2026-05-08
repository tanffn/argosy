"use client";

import { useEffect, useState } from "react";

interface HealthResponse {
  status: "ok" | "error";
  db: "ok" | "error";
  version: string;
  git_sha: string;
  started_at: string;
}

/**
 * Footer badge showing the running backend's version, git SHA, startup
 * time, and DB status. Polls /api/health on mount and refreshes the
 * relative-time string every 30s. Verifies "is the running backend
 * on the commit I just pushed?" at a glance.
 */
export function VersionBadge() {
  const [info, setInfo] = useState<HealthResponse | null>(null);
  const [error, setError] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/health", { cache: "no-store" })
      .then((r) => r.json() as Promise<HealthResponse>)
      .then((j) => {
        if (!cancelled) setInfo(j);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    const i = setInterval(() => setTick((t) => t + 1), 30_000);
    return () => {
      cancelled = true;
      clearInterval(i);
    };
  }, []);

  if (error) {
    return (
      <span className="font-mono text-rose-500">
        Argosy · backend unreachable
      </span>
    );
  }
  if (!info) {
    return <span className="font-mono">Argosy v…</span>;
  }

  const startedAt = new Date(info.started_at);
  void tick; // re-render every 30s for relative-time refresh
  const startedRel = formatRelative(startedAt);
  const dbOk = info.db === "ok";
  const dbBadge = dbOk ? "" : " · DB ✗";

  return (
    <span
      className={`font-mono ${dbOk ? "" : "text-rose-500"}`}
      title={`Started ${startedAt.toLocaleString()}\nDB ${info.db}\nGit ${info.git_sha}`}
    >
      Argosy v{info.version} ·{" "}
      <span className="text-foreground/80">{info.git_sha}</span> · started{" "}
      {startedRel}
      {dbBadge}
    </span>
  );
}

function formatRelative(d: Date): string {
  const ms = Date.now() - d.getTime();
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const days = Math.floor(h / 24);
  return `${days}d ago`;
}
