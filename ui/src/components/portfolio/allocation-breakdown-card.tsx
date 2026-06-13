"use client";

import { useEffect, useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type AllocationBreakdownDTO } from "@/lib/api";

function fmtK(k: number): string {
  if (Math.abs(k) >= 1000) return `$${(k / 1000).toFixed(2)}M`;
  return `$${k.toFixed(0)}K`;
}

/**
 * /portfolio: LIVE current allocation (your actual holdings, grouped by asset
 * class) vs the canonical plan's class targets, with a per-symbol drill-down.
 * Click a class row to see exactly which symbols fell into it (name, value, %).
 * This is the real "current vs plan target" — not the plan glide's modelled
 * today-anchor.
 */
export function AllocationBreakdownCard({ userId = "ariel" }: { userId?: string }) {
  const [data, setData] = useState<AllocationBreakdownDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  // NVDA (~61% of the book) flattens every other class to a sliver, so the
  // diversified core is unreadable. Default to excluding it; toggle to include.
  const [excludeNvda, setExcludeNvda] = useState(true);

  useEffect(() => {
    api
      .portfolioAllocationBreakdown(userId, excludeNvda)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [userId, excludeNvda]);

  if (error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Current allocation vs plan target</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-destructive">Failed to load: {error}</p>
        </CardContent>
      </Card>
    );
  }
  if (!data || data.rows.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <CardTitle>Current allocation vs plan target</CardTitle>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground whitespace-nowrap cursor-pointer select-none">
            <input
              type="checkbox"
              checked={excludeNvda}
              onChange={(e) => setExcludeNvda(e.target.checked)}
              className="accent-primary"
            />
            Exclude NVDA
          </label>
        </div>
        <CardDescription>
          Your live holdings by asset class vs the canonical plan target. Click a
          class to see its symbols. Total {fmtK(data.total_value_k)}
          {excludeNvda ? " (NVDA excluded)" : ""}.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="flex flex-col gap-2 text-sm font-mono">
          {data.rows.map((r) => {
            const actual = r.current_pct;
            const target = r.target_pct ?? 0;
            const max = Math.max(actual, target, 1);
            const over = r.target_pct !== null && actual > target + 1;
            const under = r.target_pct !== null && actual < target - 1;
            return (
              <li key={r.label} className="flex flex-col gap-1">
                <button
                  type="button"
                  onClick={() => setOpen(open === r.label ? null : r.label)}
                  aria-expanded={open === r.label}
                  className="flex items-center justify-between gap-2 text-left hover:opacity-80"
                >
                  <span className="flex items-center gap-2">
                    <span aria-hidden className="text-muted-foreground">
                      {open === r.label ? "▾" : "▸"}
                    </span>
                    <span>{r.label}</span>
                  </span>
                  <span className="text-muted-foreground tabular-nums">
                    {actual.toFixed(1)}% /{" "}
                    {r.target_pct === null ? "—" : `${target.toFixed(1)}%`}
                    {over && <span className="text-warning"> ▲ over</span>}
                    {under && <span className="text-primary"> ▼ under</span>}
                  </span>
                </button>
                <div className="flex h-2 gap-0.5 bg-muted/30 rounded">
                  <div
                    className="bg-primary/70 rounded-l"
                    style={{ width: `${(actual / max) * 50}%` }}
                  />
                  <div
                    className="bg-success/60 rounded-r"
                    style={{ width: `${(target / max) * 50}%` }}
                  />
                </div>
                {open === r.label && (
                  <div className="mt-1 ml-5 flex flex-col gap-0.5 text-xs text-muted-foreground">
                    {r.holdings.map((h, i) => (
                      <div
                        key={`${h.symbol}-${i}`}
                        className="flex items-center justify-between gap-2"
                      >
                        <span>
                          <span className="text-foreground">{h.symbol}</span>
                          {h.name ? ` · ${h.name}` : ""}
                        </span>
                        <span className="tabular-nums">
                          {fmtK(h.value_k)} · {h.pct.toFixed(1)}%
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
        <p className="mt-3 text-[11px] text-muted-foreground">{data.note}</p>
      </CardContent>
    </Card>
  );
}
