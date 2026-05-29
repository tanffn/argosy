"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { ExportPlanButton } from "@/components/plan/export-plan-button";
import { PerPositionThesisSection } from "@/components/positions/per-position-thesis-section";
import { GenerateTsvCard } from "@/components/portfolio/generate-tsv-card";
import { PortfolioSnapshotUploadCard } from "@/components/portfolio/snapshot-upload-card";
import { UnallocatedCashCard } from "@/components/portfolio/unallocated-cash-card";
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
  type PositionThesisDTO,
} from "@/lib/api";

const USER_ID = "ariel";

// Hold/Buy/Sell column on per-account tables (2026-05-29). Verdict
// comes from the per-position thesis on the current accepted plan
// draft. Tones picked so the most-frequent verdicts (HOLD on most
// positions) read as neutral; only BUY/ADD (green) and TRIM/SELL
// (rose) draw the eye.
const VERDICT_CLASS: Record<PositionThesisDTO["verdict"], string> = {
  HOLD: "text-muted-foreground border-border/40 bg-secondary/40",
  BUY: "text-emerald-400 border-emerald-400/40 bg-emerald-400/10",
  ADD: "text-emerald-400 border-emerald-400/40 bg-emerald-400/10",
  TRIM: "text-amber-400 border-amber-400/40 bg-amber-400/10",
  SELL: "text-rose-400 border-rose-400/40 bg-rose-400/10",
};

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
  // Per-position thesis cache for the Verdict column on per-account
  // tables. Fetched once on mount; null when the plan-draft endpoint
  // has nothing for the user (fresh install / 404 from upstream).
  const [thesisByTicker, setThesisByTicker] = useState<
    Record<string, PositionThesisDTO>
  >({});

  useEffect(() => {
    api
      .portfolioSnapshot(USER_ID)
      .then((data) => setSnap(data))
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    // Fail soft: a 404 (no current accepted plan) just means the
    // Verdict column shows "—" for every row; not an error state.
    api
      .positionTheses(USER_ID)
      .then((rows) => {
        const map: Record<string, PositionThesisDTO> = {};
        for (const r of rows) map[r.ticker] = r;
        setThesisByTicker(map);
      })
      .catch(() => {
        // swallow
      });
  }, []);

  const groups = useMemo(() => groupByAccount(snap), [snap]);

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Portfolio</h1>
          <p className="text-sm text-muted-foreground">
            {snap?.snapshot_date
              ? `Snapshot: ${snap.snapshot_date}`
              : "No portfolio snapshot found."}
          </p>
        </div>
        <ExportPlanButton userId={USER_ID} />
      </header>

      {loading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && <p className="text-sm text-error font-mono">{error}</p>}

      {/* Monthly portfolio-snapshot upload tile (2026-05-29). User
         drops the monthly Family Finances Status TSV; the route
         persists under ARGOSY_EXPENSE_SAMPLES_ROOT and fires the
         windfall detector inline. See
         argosy/api/routes/portfolio.py::upload_snapshot. */}
      {/* Argosy-generates-the-TSV (2026-05-29): primary path for
         composing the canonical Family Finances Status TSV from
         current state. Sits above the upload tile so the user's
         first instinct is "generate" rather than "upload". The
         upload tile remains the input flow for fresh Leumi XLS. */}
      <GenerateTsvCard
        userId={USER_ID}
        onGenerated={() => {
          api
            .portfolioSnapshot(USER_ID)
            .then((data) => setSnap(data))
            .catch((e: unknown) => setError(String(e)));
        }}
      />

      <PortfolioSnapshotUploadCard
        userId={USER_ID}
        onUploadComplete={() => {
          // Re-fetch the snapshot so the page reflects the just-uploaded data.
          api
            .portfolioSnapshot(USER_ID)
            .then((data) => setSnap(data))
            .catch((e: unknown) => setError(String(e)));
        }}
      />

      {/* Unallocated-cash proposal (2026-05-29). Self-tuning trigger
         based on the plan-target cash row -- fires when current cash
         exceeds plan target by ~1.5x. Renders nothing when no overage.
         See argosy/services/unallocated_cash_detector.py. */}
      <UnallocatedCashCard userId={USER_ID} />

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
                  <th className="py-2 text-right">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {g.positions.map((p, i) => {
                  const t = (p.symbol || "").toUpperCase();
                  const thesis = t ? thesisByTicker[t] : undefined;
                  return (
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
                      <td className="py-1.5 text-right">
                        {thesis ? (
                          <Link
                            href="/positions"
                            title={
                              `Conviction: ${thesis.conviction} — ${thesis.reasoning_md.slice(0, 200)}`
                            }
                            className={`inline-block px-2 py-0.5 rounded border text-[10px] font-medium tabular-nums hover:opacity-80 transition-opacity ${VERDICT_CLASS[thesis.verdict]}`}
                          >
                            {thesis.verdict}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground/60">—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
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
