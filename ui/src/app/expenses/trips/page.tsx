"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { TagChip } from "@/components/expenses/tag-chip";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  expensesApi,
  type CurrencyAmount,
  type TripSummary,
} from "@/lib/expenses/api";
import { colorForSlug, formatNIS } from "@/lib/expenses/format";

const USER_ID = "ariel";

function formatCurrencyAmount(c: CurrencyAmount): string {
  if (c.currency === "NIS" || c.currency === "ILS") return formatNIS(c.total);
  // Best-effort foreign formatter; round to 2dp.
  return `${c.total.toFixed(2)} ${c.currency}`;
}

export default function TripsPage() {
  const [tags, setTags] = useState<string[]>([]);
  const [summaries, setSummaries] = useState<Record<string, TripSummary>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const r = await expensesApi.listTags(USER_ID, "trip:");
        if (cancelled) return;
        setTags(r.tags);
        const entries = await Promise.all(
          r.tags.map((t) =>
            expensesApi.tripSummary(USER_ID, t)
              .then((s) => [t, s] as const)
              .catch(() => [t, null] as const),
          ),
        );
        if (cancelled) return;
        const map: Record<string, TripSummary> = {};
        for (const [t, s] of entries) if (s) map[t] = s;
        setSummaries(map);
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (error) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-rose-600 text-sm">
          Failed to load: {error}
        </CardContent>
      </Card>
    );
  }
  if (loading) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground text-sm">
          Loading trips…
        </CardContent>
      </Card>
    );
  }
  if (tags.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground text-sm">
          No trip tags yet. Tag a transaction with{" "}
          <code className="px-1 py-0.5 bg-secondary rounded text-xs">
            trip:greece-2026-aug
          </code>{" "}
          from the Transactions page to start grouping flights, hotels and
          restaurants under one trip.
        </CardContent>
      </Card>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-xl font-semibold">Trips</h2>
        <div className="text-sm text-muted-foreground">
          Each card aggregates flights, hotels, restaurants and any other
          transaction tagged with the same trip tag. Click a card to drill in.
        </div>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {tags.map((tag) => {
          const s = summaries[tag];
          if (!s) return null;
          const totalBase = s.total_nis || 1;
          const drillHref = `/expenses/transactions?tag=${encodeURIComponent(tag)}`;
          return (
            <Link key={tag} href={drillHref} className="block group">
              <Card className="h-full transition-colors group-hover:border-sky-500/40">
                <CardHeader className="pb-2">
                  <CardTitle className="flex items-center gap-2 text-sm">
                    <TagChip tag={tag} />
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-semibold">
                    {formatNIS(s.total_nis)}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {s.transaction_count} tx
                    {s.period_start && (
                      <> · {s.period_start} → {s.period_end ?? s.period_start}</>
                    )}
                  </div>
                  {s.currency_breakdown.length > 1 && (
                    <div className="mt-2 text-xs text-muted-foreground">
                      {s.currency_breakdown.map((c) => formatCurrencyAmount(c)).join(" · ")}
                    </div>
                  )}
                  {s.by_category.length > 0 && (
                    <div className="mt-3">
                      <div className="flex h-2 w-full overflow-hidden rounded">
                        {s.by_category.map((c) => (
                          <div
                            key={c.slug}
                            title={`${c.label_en}: ${formatNIS(c.total_nis)}`}
                            style={{
                              width: `${(c.total_nis / totalBase) * 100}%`,
                              backgroundColor: colorForSlug(c.slug),
                            }}
                          />
                        ))}
                      </div>
                      <div className="mt-1.5 text-xs text-muted-foreground truncate">
                        {s.by_category.slice(0, 3).map((c) => c.label_en).join(" · ")}
                        {s.by_category.length > 3 && " …"}
                      </div>
                    </div>
                  )}
                </CardContent>
              </Card>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
