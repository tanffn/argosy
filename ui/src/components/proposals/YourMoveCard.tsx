"use client";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type PeriodDirectiveDTO } from "@/lib/api";

// Size-proportional round money (matches DeployCashCard's fmtMoney contract):
// small amounts in full, $10k+ in "k" notation.
function fmtUsd(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 10_000) return `$${Math.round(n / 1_000).toLocaleString()}k`;
  return `$${Math.round(n).toLocaleString()}`;
}

function fmtNis(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 10_000) return `₪${Math.round(n / 1_000).toLocaleString()}k`;
  return `₪${Math.round(n).toLocaleString()}`;
}

export function YourMoveCard({ userId }: { userId: string }) {
  const [directive, setDirective] = useState<PeriodDirectiveDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Initial load on mount. Loading starts `true`, so the effect body performs no
  // synchronous setState — state is only updated inside the async callbacks.
  useEffect(() => {
    let cancelled = false;
    api
      .periodDirective(userId, false)
      .then((d) => {
        if (!cancelled) setDirective(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Could not load your move.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  // On-demand refresh (event handler → setState is fine here): re-assess with
  // stale inputs refreshed first.
  const onRefresh = useCallback(() => {
    setRefreshing(true);
    setError(null);
    api
      .periodDirective(userId, true)
      .then(setDirective)
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : "Could not refresh your move."),
      )
      .finally(() => setRefreshing(false));
  }, [userId]);

  const sellDue = directive?.sell.status === "sell_due";
  const thesisBreak = directive?.sell.category === "thesis-break";
  const hasActions = directive?.has_actions ?? false;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle>Your move this period</CardTitle>
            <CardDescription>
              What your team recommends now — buy and sell, plan-driven.
            </CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            disabled={loading || refreshing}
            onClick={onRefresh}
            title="Refresh stale inputs (FX) and re-assess"
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </Button>
        </div>
      </CardHeader>

      <CardContent className="flex flex-col gap-4">
        {loading && <div className="text-sm text-muted-foreground">Assembling your move…</div>}

        {!loading && error && <div className="text-sm text-red-700">{error}</div>}

        {!loading && !error && directive && (
          <>
            {!hasActions && (
              <div className="rounded-md border border-border/50 bg-emerald-50 px-3 py-2.5 text-sm text-emerald-900">
                You&apos;re on track — nothing needs action this period.
                <div className="mt-1 text-xs text-emerald-800/80">{directive.sell.headline}</div>
              </div>
            )}

            {/* BUY */}
            {directive.buy.items.length > 0 && (
              <section>
                <div className="mb-1.5 flex items-center justify-between">
                  <h3 className="text-sm font-semibold">Deploy your cash</h3>
                  {directive.buy.excess_usd > 0 && (
                    <span className="text-xs text-muted-foreground">
                      {fmtUsd(directive.buy.excess_usd)} deployable
                    </span>
                  )}
                </div>
                {directive.buy.headline && (
                  <p className="mb-2 text-xs text-muted-foreground">{directive.buy.headline}</p>
                )}
                <ul className="divide-y divide-border/40 rounded-md border border-border/50">
                  {directive.buy.items.map((it, i) => (
                    <li key={`${it.instrument}-${i}`} className="flex items-start gap-3 px-3 py-2.5">
                      <div className="flex-1">
                        <div className="flex items-center gap-2">
                          <span className="font-medium">{it.instrument}</span>
                          <span className="text-xs text-muted-foreground">{it.asset_class}</span>
                          {it.tier === "high" && (
                            <Badge variant="secondary" className="text-[11px]">
                              high-potential
                            </Badge>
                          )}
                        </div>
                        {it.rationale && (
                          <div className="mt-0.5 text-xs text-muted-foreground">{it.rationale}</div>
                        )}
                      </div>
                      <div className="whitespace-nowrap text-sm font-semibold">
                        {fmtUsd(it.amount_usd)}
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {/* SELL — amber for the routine glide, red when a thesis break
                accelerates it. */}
            {sellDue && (
              <section
                className={
                  thesisBreak
                    ? "rounded-md border border-red-300/70 bg-red-50 px-3 py-2.5"
                    : "rounded-md border border-amber-300/70 bg-amber-50 px-3 py-2.5"
                }
              >
                <div className="flex items-center justify-between">
                  <h3
                    className={
                      thesisBreak
                        ? "text-sm font-semibold text-red-900"
                        : "text-sm font-semibold text-amber-900"
                    }
                  >
                    {thesisBreak ? "Reduce NVDA — thesis flagged" : "Trim NVDA (your glide)"}
                  </h3>
                  <div
                    className={
                      thesisBreak
                        ? "whitespace-nowrap text-sm font-semibold text-red-900"
                        : "whitespace-nowrap text-sm font-semibold text-amber-900"
                    }
                  >
                    {fmtNis(directive.sell.tranche_nis)}
                  </div>
                </div>
                <p
                  className={
                    thesisBreak ? "mt-1 text-xs text-red-900/90" : "mt-1 text-xs text-amber-900/90"
                  }
                >
                  {directive.sell.headline}
                </p>
                {directive.sell.tax_note && (
                  <p
                    className={
                      thesisBreak ? "mt-1 text-xs text-red-800/80" : "mt-1 text-xs text-amber-800/80"
                    }
                  >
                    {directive.sell.tax_note}
                  </p>
                )}
                {directive.sell.notes.map((n, i) => (
                  <p key={i} className="mt-1 text-xs text-muted-foreground">
                    {n}
                  </p>
                ))}
              </section>
            )}

            {/* Freshness caveats */}
            {(directive.freshness.fx_stale || directive.freshness.discovery_stale) && (
              <div className="flex flex-wrap gap-2">
                {directive.freshness.fx_stale && (
                  <Badge variant="secondary" className="text-[11px]">
                    FX may be stale — tap Refresh
                  </Badge>
                )}
                {directive.freshness.discovery_stale && (
                  <Badge variant="secondary" className="text-[11px]">
                    Discovery picks {directive.freshness.discovery_stale_days}d old
                  </Badge>
                )}
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
