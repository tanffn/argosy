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
import { api, type TrendRadarDTO } from "@/lib/api";

function fmtMoney(x: number | null): string {
  if (x === null || x === undefined) return "?";
  const a = Math.abs(x);
  if (a >= 1e9) return `$${(x / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `$${(x / 1e6).toFixed(0)}M`;
  if (a >= 1e3) return `$${(x / 1e3).toFixed(0)}K`;
  return `$${x.toFixed(2)}`;
}

function familyTone(f: string): "success" | "secondary" | "outline" {
  if (f === "MOMENTUM") return "success";
  if (f === "GROWTH") return "secondary";
  return "outline";
}

/**
 * /proposals tile: the live trend radar — high-risk SOURCING for the sleeve's
 * single-name carve-out. Cross-source signal (momentum / attention / growth),
 * pump-guarded (>=2 families) + liquidity-filtered to a satellite cap band.
 * On-demand because each scan hits live network sources (~5s).
 *
 * These are NOT recommendations: every name needs the speculative monitor +
 * a stop-loss before acting, and a backtest before trusting the signal.
 */
export function TrendRadarCard() {
  const [data, setData] = useState<TrendRadarDTO | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = () => {
    setLoading(true);
    setError(null);
    api
      .portfolioTrendRadar(15)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };

  return (
    <Card className="border-warning/30">
      <CardHeader>
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <CardTitle className="text-base font-mono">Trend radar &mdash; high-potential sourcing</CardTitle>
            <CardDescription className="mt-1">
              Live cross-source scan (momentum / attention / growth), pump-guarded and
              liquidity-filtered. High-risk single names &mdash; pair each with a stop-loss
              and backtest before committing capital.
            </CardDescription>
          </div>
          <Button onClick={run} disabled={loading} size="sm" variant="outline">
            {loading ? "Scanning…" : data ? "Re-scan" : "Scan now"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {error && <div className="text-xs text-destructive">Scan failed: {error}</div>}
        {!data && !error && (
          <div className="text-xs text-muted-foreground">
            Click &ldquo;Scan now&rdquo; to fan out across the signal sources (~5s).
          </div>
        )}
        {data &&
          data.shortlist.map((c, i) => (
            <div
              key={c.ticker}
              className="rounded-md border border-border bg-secondary/30 px-3 py-2 text-xs"
            >
              <div className="flex items-center gap-2 flex-wrap font-mono">
                <span className="text-muted-foreground">#{i + 1}</span>
                <span className="font-semibold text-sm">{c.ticker}</span>
                <span className="text-muted-foreground">{c.name}</span>
                <Badge variant="secondary">score {c.score.toFixed(0)}</Badge>
                {c.families.map((f) => (
                  <Badge key={f} variant={familyTone(f)} className="text-[10px]">
                    {f}
                  </Badge>
                ))}
              </div>
              <div className="mt-1 flex gap-3 flex-wrap text-muted-foreground">
                <span>price {fmtMoney(c.price)}</span>
                <span>cap {fmtMoney(c.market_cap)}</span>
                <span>$vol/d {fmtMoney(c.dollar_volume)}</span>
                {c.pct_change !== null && (
                  <span>{c.pct_change >= 0 ? "+" : ""}{c.pct_change.toFixed(1)}%</span>
                )}
                {c.reasons.length > 0 && <span>· {c.reasons.slice(0, 3).join("; ")}</span>}
              </div>
            </div>
          ))}
        {data && (
          <div className="mt-2 text-[11px] text-muted-foreground">
            {data.shortlist.length} surfaced · {data.quarantine_count} quarantined by the
            pump/liquidity guards. {data.note}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
