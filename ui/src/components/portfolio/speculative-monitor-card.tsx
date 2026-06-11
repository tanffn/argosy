"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type MonitorSignalDTO, type SpeculativeMonitorDTO } from "@/lib/api";

function actionTone(a: string): "destructive" | "secondary" | "outline" | "success" {
  if (a === "SELL") return "destructive";
  if (a === "WATCH") return "secondary";
  if (a === "TRIM") return "outline";
  return "success";
}

function Signal({ s }: { s: MonitorSignalDTO }) {
  return (
    <div className="rounded-md border border-border bg-secondary/30 px-3 py-2 text-xs">
      <div className="flex items-center gap-2 flex-wrap font-mono">
        <span className="font-semibold text-sm">{s.ticker}</span>
        <Badge variant={actionTone(s.action)}>{s.action}</Badge>
        <span className="text-muted-foreground">
          ${s.current_price.toFixed(2)} · {s.pct_from_entry >= 0 ? "+" : ""}
          {s.pct_from_entry.toFixed(1)}% vs entry · {s.pct_from_peak.toFixed(1)}% from peak
        </span>
        <span className="text-foreground">
          stop ${s.binding_stop_level.toFixed(2)} ({s.distance_to_stop_pct >= 0 ? "+" : ""}
          {s.distance_to_stop_pct.toFixed(1)}% away)
        </span>
      </div>
      <div className="mt-1 text-muted-foreground leading-relaxed">{s.reason}</div>
    </div>
  );
}

/**
 * /proposals tile: the daily exit-discipline read on the high-risk single
 * names. Hard stop + trailing stop + 50d-MA momentum break per position; the
 * user's "tell me when to sell / set a stop-loss" ask. On-demand here; the
 * scheduler re-checks daily.
 */
export function SpeculativeMonitorCard({ tickers = "" }: { tickers?: string }) {
  const [data, setData] = useState<SpeculativeMonitorDTO | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = () => {
    setLoading(true);
    setError(null);
    api
      .portfolioSpeculativeMonitor(tickers)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };

  const order = { SELL: 0, WATCH: 1, TRIM: 2, HOLD: 3 } as const;

  return (
    <Card className="border-destructive/30">
      <CardHeader>
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <CardTitle className="text-base font-mono">Speculative monitor &mdash; stop-loss / sell signals</CardTitle>
            <CardDescription className="mt-1">
              Mechanical exit discipline on the high-risk names: hard stop (
              {data ? `${data.hard_stop_pct.toFixed(0)}%` : "20%"} from entry) + trailing stop (
              {data ? `${data.trailing_stop_pct.toFixed(0)}%` : "25%"} from peak) + a 50-day-MA
              momentum break. Re-checked daily by the scheduler.
            </CardDescription>
          </div>
          <Button onClick={run} disabled={loading} size="sm" variant="outline">
            {loading ? "Checking…" : data ? "Refresh" : "Check now"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {error && <div className="text-xs text-destructive">Check failed: {error}</div>}
        {!data && !error && (
          <div className="text-xs text-muted-foreground">
            Click &ldquo;Check now&rdquo; for live stop levels and sell signals.
          </div>
        )}
        {data &&
          [...data.signals]
            .sort((a, b) => order[a.action] - order[b.action])
            .map((s) => <Signal key={s.ticker} s={s} />)}
        {data && <div className="mt-2 text-[11px] text-muted-foreground">{data.note}</div>}
      </CardContent>
    </Card>
  );
}
