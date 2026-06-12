"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type DiscoveryDTO } from "@/lib/api";

function convictionTone(c: string): "success" | "secondary" | "outline" {
  if (c === "HIGH") return "success";
  if (c === "MED") return "secondary";
  return "outline";
}

function verdictTone(v: string): "success" | "secondary" | "destructive" {
  if (v === "BUY") return "success";
  if (v === "WATCH") return "secondary";
  return "destructive";
}

function fmtWhen(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

/**
 * /proposals tile: the combined high-potential DISCOVERY surface — fleet-graded
 * picks (radar → cheap estimator triage → Opus fleet grade) plus the estimator
 * shortlist. Conviction/verdict only (no dollar sizing). The cached highlights
 * load instantly; "Refresh" runs the funnel (smart — only new/changed names are
 * re-researched). Click a pick to expand its thesis.
 *
 * These are NOT recommendations: high-risk single names; pair with a stop-loss.
 */
export function DiscoveryCard() {
  const [data, setData] = useState<DiscoveryDTO | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    api
      .portfolioDiscovery()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const refresh = () => {
    setLoading(true);
    setError(null);
    api
      .portfolioDiscoveryRefresh(false)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };

  const picks = data?.picks ?? [];
  const watch = (data?.estimated ?? []).filter(
    (e) => e.go && !picks.some((p) => p.ticker === e.ticker),
  );

  return (
    <Card className="border-warning/30">
      <CardHeader>
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <CardTitle className="text-base font-mono">
              High-potential discovery
            </CardTitle>
            <CardDescription className="mt-1">
              Fleet-graded growth ideas (radar → estimator → fleet). Conviction,
              not dollar sizing — pair each with a stop-loss before acting.
              <span className="block mt-0.5 text-[11px]">
                Last refreshed: {fmtWhen(data?.last_refreshed_at ?? null)}
              </span>
            </CardDescription>
          </div>
          <Button onClick={refresh} disabled={loading} size="sm" variant="outline">
            {loading ? "Refreshing…" : "Refresh"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {error && (
          <div className="text-xs text-destructive">Discovery failed: {error}</div>
        )}
        {data && picks.length === 0 && watch.length === 0 && !error && (
          <div className="text-xs text-muted-foreground">
            No graded picks yet. Click &ldquo;Refresh&rdquo; to run the discovery
            funnel (sources → triage → fleet grade).
          </div>
        )}

        {picks.map((p) => (
          <button
            key={p.ticker}
            type="button"
            onClick={() => setOpen(open === p.ticker ? null : p.ticker)}
            className="w-full text-left rounded-md border border-border bg-secondary/30 px-3 py-2 text-xs hover:bg-secondary/50"
            aria-expanded={open === p.ticker}
          >
            <div className="flex items-center gap-2 flex-wrap font-mono">
              <span className="font-semibold text-sm">{p.ticker}</span>
              <Badge variant={verdictTone(p.verdict)}>{p.verdict}</Badge>
              <Badge variant={convictionTone(p.conviction)} className="text-[10px]">
                {p.conviction} conviction
              </Badge>
              <span className="ml-auto text-muted-foreground">
                {open === p.ticker ? "▾" : "▸"} rationale
              </span>
            </div>
            {open === p.ticker && (
              <div className="mt-2 whitespace-pre-wrap text-muted-foreground">
                {p.thesis_md}
                {p.cites.length > 0 && (
                  <div className="mt-1 text-[10px]">
                    sources: {p.cites.join(", ")}
                  </div>
                )}
              </div>
            )}
          </button>
        ))}

        {watch.length > 0 && (
          <div className="pt-1">
            <div className="text-[11px] font-semibold text-muted-foreground">
              On the radar (estimator go, not yet fleet-graded)
            </div>
            {watch.map((e) => (
              <div
                key={e.ticker}
                className="mt-1 flex items-center gap-2 flex-wrap rounded-md border border-border/60 px-3 py-1.5 text-xs font-mono"
              >
                <span className="font-semibold">{e.ticker}</span>
                <Badge variant={convictionTone(e.conviction)} className="text-[10px]">
                  {e.conviction}
                </Badge>
                <span className="text-muted-foreground">
                  sentiment {e.sentiment >= 0 ? "+" : ""}
                  {e.sentiment.toFixed(2)}
                </span>
                <span className="text-muted-foreground">· {e.one_line}</span>
              </div>
            ))}
          </div>
        )}

        {data && (
          <div className="mt-2 text-[11px] text-muted-foreground">{data.note}</div>
        )}
      </CardContent>
    </Card>
  );
}
