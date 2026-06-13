"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { ExportPlanButton } from "@/components/plan/export-plan-button";
import { PerPositionThesisSection } from "@/components/positions/per-position-thesis-section";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { AllocationBreakdownCard } from "@/components/portfolio/allocation-breakdown-card";
import { GenerateTsvCard } from "@/components/portfolio/generate-tsv-card";
import { PortfolioSnapshotUploadCard } from "@/components/portfolio/snapshot-upload-card";
import { RealEstateCard } from "@/components/portfolio/real-estate-card";
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

// The NVDA RSU row's location is bare "schwab" while the other Schwab
// holdings are "schwab 876" — same account, so group them together.
function normalizeLocation(loc: string): string {
  const l = (loc || "").trim();
  if (l.toLowerCase() === "schwab") return "schwab 876";
  return l || "(unknown)";
}

function isRealEstate(p: PortfolioPosition): boolean {
  return (p.asset_type || "").trim().toLowerCase() === "real estate";
}

function groupByAccount(snap: PortfolioSnapshotDTO | null): AccountGroup[] {
  if (!snap) return [];
  const map = new Map<string, AccountGroup>();
  for (const p of snap.positions) {
    // Real estate is surfaced in its own net-worth card, not as a brokerage
    // account — keep it out of the per-account tables + liquid total.
    if (isRealEstate(p)) continue;
    const key = normalizeLocation(p.location);
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
  // Liquid investable total = sum of the (real-estate-excluded) account
  // groups, so the "Total liquid USD" stat reconciles with the tables below
  // it. Real-estate net worth is shown separately in its own card.
  const liquidTotalK = useMemo(
    () => groups.reduce((s, g) => s + g.total_usd_k, 0),
    [groups],
  );

  // Per-account table sorting (applies to every account table). Click a
  // sortable header to sort; click again to flip direction.
  type SortKey = "symbol" | "type" | "value" | "verdict";
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "value" ? "desc" : "asc");
    }
  }
  // Verdict ranked by actionability (BUY/ADD → SELL); unrated sorts last.
  const VERDICT_ORDER: Record<string, number> = {
    BUY: 0, ADD: 1, HOLD: 2, TRIM: 3, SELL: 4,
  };
  function sortPositions(positions: PortfolioPosition[]): PortfolioPosition[] {
    if (!sortKey) return positions;
    const dir = sortDir === "asc" ? 1 : -1;
    const key = (p: PortfolioPosition): string | number => {
      if (sortKey === "symbol") return (p.symbol || p.details || "").toLowerCase();
      if (sortKey === "type") return (p.asset_type || "").toLowerCase();
      if (sortKey === "value") return p.usd_value_k ?? -Infinity;
      const v = thesisByTicker[(p.symbol || "").toUpperCase()]?.verdict;
      return v ? (VERDICT_ORDER[v] ?? 98) : 99;
    };
    return [...positions].sort((a, b) => {
      const av = key(a);
      const bv = key(b);
      if (av < bv) return -dir;
      if (av > bv) return dir;
      return 0;
    });
  }

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

      {/* Parse warnings surfaced (nothing hidden, nothing lost): the snapshot
          DTO has always carried parse_warnings; this renders them so a row the
          parser couldn't fully read is visible rather than silently dropped. */}
      {snap?.parse_warnings && snap.parse_warnings.length > 0 && (
        <div className="rounded-md border border-amber-400/40 bg-amber-400/10 p-3">
          <p className="text-sm font-medium text-amber-300">
            ⚠ {snap.parse_warnings.length} parse warning
            {snap.parse_warnings.length > 1 ? "s" : ""} on this snapshot —
            surfaced, not dropped silently:
          </p>
          <ul className="mt-1 list-disc pl-5 text-xs font-mono text-amber-200/90 space-y-0.5">
            {snap.parse_warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

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
      {/* One "Update portfolio data" panel: generate a fresh snapshot from
         Argosy state, or upload a monthly bank statement. Both refresh the
         page's snapshot on completion. */}
      <Card>
        <CardHeader>
          <CardTitle>Update portfolio data</CardTitle>
          <CardDescription>
            Generate a fresh snapshot from current Argosy state, or upload a
            monthly Leumi/Schwab statement.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <GenerateTsvCard
            embedded
            userId={USER_ID}
            onGenerated={() => {
              api
                .portfolioSnapshot(USER_ID)
                .then((data) => setSnap(data))
                .catch((e: unknown) => setError(String(e)));
            }}
          />
          <div className="border-t border-border/60" />
          <PortfolioSnapshotUploadCard
            embedded
            userId={USER_ID}
            onUploadComplete={() => {
              // Re-fetch the snapshot so the page reflects the just-uploaded data.
              api
                .portfolioSnapshot(USER_ID)
                .then((data) => setSnap(data))
                .catch((e: unknown) => setError(String(e)));
            }}
          />
        </CardContent>
      </Card>

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
                ${Math.round(liquidTotalK).toLocaleString()}K
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
                <tr className="text-left text-xs text-muted-foreground border-b border-border select-none">
                  <th
                    className="py-2 cursor-pointer hover:text-foreground"
                    onClick={() => toggleSort("symbol")}
                  >
                    Symbol{sortKey === "symbol" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                  </th>
                  <th
                    className="py-2 cursor-pointer hover:text-foreground"
                    onClick={() => toggleSort("type")}
                  >
                    Type{sortKey === "type" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                  </th>
                  <th className="py-2">Estate</th>
                  <th className="py-2 text-right">Shares</th>
                  <th className="py-2 text-right">Price</th>
                  <th
                    className="py-2 text-right cursor-pointer hover:text-foreground"
                    onClick={() => toggleSort("value")}
                  >
                    Value (K USD){sortKey === "value" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                  </th>
                  <th
                    className="py-2 text-right cursor-pointer hover:text-foreground"
                    onClick={() => toggleSort("verdict")}
                  >
                    Verdict{sortKey === "verdict" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                  </th>
                </tr>
              </thead>
              <tbody>
                {sortPositions(g.positions).map((p, i) => {
                  const t = (p.symbol || "").toUpperCase();
                  const thesis = t ? thesisByTicker[t] : undefined;
                  const isCash = (p.asset_type || "").toLowerCase() === "cash";
                  const symbolLabel = isCash
                    ? `Cash (${(p.currency || "").toUpperCase() || "—"})`
                    : p.symbol || p.details || "—";
                  return (
                    <tr
                      key={`${p.location}-${p.symbol || p.details}-${i}`}
                      className="border-b border-border/40"
                    >
                      <td className="py-1.5">{symbolLabel}</td>
                      <td className="py-1.5 text-muted-foreground">{p.asset_type}</td>
                      <td className="py-1.5">
                        {p.estate_safe === null ? (
                          <span className="text-muted-foreground/50">—</span>
                        ) : p.estate_safe ? (
                          <span
                            className="text-[10px] text-emerald-400/80"
                            title="Estate-safe — non-US-situs (UCITS / Israeli domicile)"
                          >
                            ✓ safe
                          </span>
                        ) : (
                          <span
                            className="text-[10px] text-amber-400"
                            title="US-situs — exposed to US estate tax (40% above $60k) for a non-US person"
                          >
                            ⚠ US-situs
                          </span>
                        )}
                      </td>
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

      {/* Real-estate net worth (4 properties, net of mortgage) — separate
         from the investable book per the four-surface model. */}
      <RealEstateCard userId={USER_ID} />

      <CollapsibleSection
        title="Per-position thesis"
        summary="Hold / Buy / Trim / Sell verdict + conviction per holding (plan-derived)"
      >
        <PerPositionThesisSection userId={USER_ID} />
      </CollapsibleSection>

      {/* Live current allocation (your real holdings by class) vs the canonical
          plan target, with per-symbol drill-down — replaces the prior chart that
          compared the plan glide's modelled today-anchor to its end-state. */}
      <AllocationBreakdownCard userId={USER_ID} />
    </main>
  );
}
