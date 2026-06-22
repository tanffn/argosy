"use client";

// Decision-funnel runs (debug) — lists the daily funnel runs and lands on
// /decisions/funnel/[id] for the full per-stage trace + immutable snapshots.
// This is a DEBUG surface (under the Decisions tab), not a client surface, so
// it intentionally exposes internal fields (shadow flag, policy/IPS version,
// per-stage totals). Data: GET /api/decisions/funnel/runs.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import { api, type FunnelRunSummary } from "@/lib/api";

const USER_ID = "ariel";

function parseAsUTC(iso: string | null): number {
  if (!iso) return NaN;
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  return Date.parse(hasTz ? iso : iso + "Z");
}

function fmt(iso: string | null): string {
  const ms = parseAsUTC(iso);
  if (Number.isNaN(ms)) return iso ?? "—";
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
}

function statusTone(s: string): "success" | "warning" | "error" | "neutral" | "accent" {
  if (s === "ok") return "success";
  if (s === "error" || s === "killed") return "error";
  if (s === "running") return "accent";
  return "neutral";
}

function n(totals: Record<string, number | boolean>, key: string): number {
  const v = totals?.[key];
  return typeof v === "number" ? v : 0;
}

export default function FunnelRunsPage() {
  const [rows, setRows] = useState<FunnelRunSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await api.funnelRuns(USER_ID, 50);
      setRows(data.runs);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRows(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    refresh();
  }, [refresh]);

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <div className="flex items-center gap-3">
          <Link href="/decisions" className="text-sm text-muted-foreground hover:underline">
            ← Decisions
          </Link>
        </div>
        <h1 className="text-2xl font-semibold tracking-tight mt-1">
          Decision funnel runs (debug)
        </h1>
        <p className="text-sm text-muted-foreground">
          Daily tiered decision-funnel runs. Each run is fully traced for replay
          — click a row to see every name considered (routed / dropped / no-op /
          proposed), the signal or rule that fired, the model + tokens, and the
          immutable per-decision snapshots.
        </p>
      </header>

      {error && (
        <Card>
          <CardContent className="py-6 text-sm text-error font-mono">
            Couldn&apos;t load funnel runs — {error}
          </CardContent>
        </Card>
      )}

      {!error && rows !== null && rows.length === 0 && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No funnel runs yet. The funnel is gated by
            ARGOSY_DECISION_FUNNEL_ENABLED (off by default).
          </CardContent>
        </Card>
      )}

      {!loading && !error && rows !== null && rows.length > 0 && (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-[10px] uppercase tracking-wide text-muted-foreground border-b border-border">
                  <th className="text-left py-2 px-3">Run</th>
                  <th className="text-left py-2 px-3">Started</th>
                  <th className="text-left py-2 px-3">Status</th>
                  <th className="text-left py-2 px-3">Mode</th>
                  <th className="text-right py-2 px-3">Routed</th>
                  <th className="text-right py-2 px-3">Triaged</th>
                  <th className="text-right py-2 px-3">Proposed</th>
                  <th className="text-right py-2 px-3">Surfaced</th>
                  <th className="text-left py-2 px-3">Policy / IPS</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.run_id} className="border-b border-border/40 hover:bg-secondary/40">
                    <td className="py-2 px-3">
                      <Link
                        href={`/decisions/funnel/${r.run_id}`}
                        className="font-mono font-medium hover:underline"
                      >
                        #{r.run_id}
                      </Link>
                    </td>
                    <td className="py-2 px-3 text-xs font-mono text-muted-foreground whitespace-nowrap">
                      {fmt(r.started_at)}
                    </td>
                    <td className="py-2 px-3">
                      <StatusPill tone={statusTone(r.status)} mono>
                        {r.status}
                      </StatusPill>
                    </td>
                    <td className="py-2 px-3">
                      <Badge variant={r.shadow ? "outline" : "secondary"} className="text-[10px]">
                        {r.shadow ? "shadow" : "live"}
                      </Badge>
                    </td>
                    <td className="py-2 px-3 text-right tabular-nums">{n(r.totals, "stage1_routed")}</td>
                    <td className="py-2 px-3 text-right tabular-nums">{n(r.totals, "stage2_go")}</td>
                    <td className="py-2 px-3 text-right tabular-nums">{n(r.totals, "stage3_proposed")}</td>
                    <td className="py-2 px-3 text-right tabular-nums">{n(r.totals, "surfaced")}</td>
                    <td className="py-2 px-3 text-[11px] font-mono text-muted-foreground whitespace-nowrap">
                      {r.policy_version ?? "—"} / {r.ips_version ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
    </main>
  );
}
