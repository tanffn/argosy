"use client";

import { useEffect, useMemo, useState } from "react";

import { PerPositionThesisSection } from "@/components/positions/per-position-thesis-section";
import { WealthDashboard } from "@/components/portfolio/wealth-dashboard";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type PortfolioPosition,
  type PortfolioSnapshotDTO,
} from "@/lib/api";

const USER_ID = "ariel";

interface AccountGroup {
  location: string;
  positions: PortfolioPosition[];
  total_usd_k: number;
}

function groupByAccount(snap: PortfolioSnapshotDTO | null): AccountGroup[] {
  if (!snap) return [];
  const map = new Map<string, AccountGroup>();
  for (const p of snap.positions) {
    const key = p.location || "(unknown)";
    const g = map.get(key) ?? { location: key, positions: [], total_usd_k: 0 };
    g.positions.push(p);
    g.total_usd_k += p.usd_value_k ?? 0;
    map.set(key, g);
  }
  return Array.from(map.values()).sort(
    (a, b) => b.total_usd_k - a.total_usd_k,
  );
}

export default function PortfolioPage() {
  const [snap, setSnap] = useState<PortfolioSnapshotDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .portfolioSnapshot(USER_ID)
      .then((data) => setSnap(data))
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const groups = useMemo(() => groupByAccount(snap), [snap]);

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Portfolio</h1>
        <p className="text-sm text-muted-foreground">
          {snap?.snapshot_date
            ? `Snapshot: ${snap.snapshot_date}`
            : "No portfolio snapshot found."}
        </p>
      </header>

      {loading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && <p className="text-sm text-error font-mono">{error}</p>}

      {/* Wealth dashboard — top-of-page retirement projection + 6 stat
         cards. Independent of the portfolio snapshot fetch above; renders
         on its own loading/error states. See
         argosy/services/wealth_dashboard.py for the aggregated payload. */}
      <WealthDashboard userId={USER_ID} />

      {snap && (
        <section className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Card>
            <CardHeader>
              <CardDescription>Total liquid USD</CardDescription>
              <CardTitle className="font-mono">
                ${snap.total_usd_value_k.toLocaleString()}K
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>USD/NIS</CardDescription>
              <CardTitle className="font-mono">
                {snap.fx_usd_nis ?? "—"}
              </CardTitle>
            </CardHeader>
          </Card>
          <Card>
            <CardHeader>
              <CardDescription>USD/EUR</CardDescription>
              <CardTitle className="font-mono">
                {snap.fx_usd_eur ?? "—"}
              </CardTitle>
            </CardHeader>
          </Card>
        </section>
      )}

      {groups.map((g) => (
        <Card key={g.location}>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>{g.location}</CardTitle>
              <span className="font-mono text-sm">
                ${g.total_usd_k.toLocaleString()}K
              </span>
            </div>
          </CardHeader>
          <CardContent>
            <table className="w-full text-sm font-mono">
              <thead>
                <tr className="text-left text-xs text-muted-foreground border-b border-border">
                  <th className="py-2">Symbol</th>
                  <th className="py-2">Type</th>
                  <th className="py-2 text-right">Shares</th>
                  <th className="py-2 text-right">Price</th>
                  <th className="py-2 text-right">Value (K USD)</th>
                </tr>
              </thead>
              <tbody>
                {g.positions.map((p, i) => (
                  <tr
                    key={`${p.location}-${p.symbol || p.details}-${i}`}
                    className="border-b border-border/40"
                  >
                    <td className="py-1.5">{p.symbol || p.details || "—"}</td>
                    <td className="py-1.5 text-muted-foreground">{p.asset_type}</td>
                    <td className="py-1.5 text-right">
                      {p.shares !== null ? p.shares.toLocaleString() : "—"}
                    </td>
                    <td className="py-1.5 text-right">
                      {p.current_price !== null ? p.current_price.toFixed(2) : "—"}
                    </td>
                    <td className="py-1.5 text-right">
                      {p.usd_value_k !== null ? p.usd_value_k.toLocaleString() : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      ))}

      <PerPositionThesisSection userId={USER_ID} withHeading />

      {snap && snap.allocations.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Allocation vs target</CardTitle>
            <CardDescription>Per category</CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-col gap-2 text-sm font-mono">
              {snap.allocations.map((a) => {
                const actual = a.pct ?? 0;
                const target = a.target_pct ?? 0;
                const max = Math.max(actual, target, 1);
                return (
                  <li key={a.category} className="flex flex-col gap-1">
                    <div className="flex items-center justify-between">
                      <span>{a.category}</span>
                      <span className="text-muted-foreground">
                        {actual.toFixed(1)}% / {target.toFixed(1)}%
                      </span>
                    </div>
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
                  </li>
                );
              })}
            </ul>
          </CardContent>
        </Card>
      )}
    </main>
  );
}
