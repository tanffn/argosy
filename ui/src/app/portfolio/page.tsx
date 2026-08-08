"use client";

import { useEffect, useMemo, useState } from "react";

import { ExportPlanButton } from "@/components/plan/export-plan-button";
import { PerPositionThesisSection } from "@/components/positions/per-position-thesis-section";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { AllocationBreakdownCard } from "@/components/portfolio/allocation-breakdown-card";
import { GenerateTsvCard } from "@/components/portfolio/generate-tsv-card";
import { HoldingHoverCard, VerdictHoverCard } from "@/components/portfolio/holding-hover-card";
import { InstrumentClassMapCard } from "@/components/portfolio/instrument-class-map-card";
import { PortfolioSnapshotUploadCard } from "@/components/portfolio/snapshot-upload-card";
import { RealEstateCard } from "@/components/portfolio/real-estate-card";
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
import {
  assertPositionsPartition,
  groupByAccount,
} from "@/lib/portfolio/position-sections";

const USER_ID = "ariel";

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
  const [holdingsReviewBusy, setHoldingsReviewBusy] = useState(false);
  const [holdingsReviewMsg, setHoldingsReviewMsg] = useState<string | null>(
    null,
  );

  const refreshTheses = () => {
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
  };

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
    refreshTheses();
  }, []);

  const groups = useMemo(() => groupByAccount(snap), [snap]);
  // Liquid investable total = sum of the (physical-RE-excluded) account
  // groups, so the "Total liquid USD" stat reconciles with the tables below
  // it. Physical real-estate net worth is shown separately in its own card;
  // listed property securities (IWDP, O, …) stay in the liquid book.
  const liquidTotalK = useMemo(
    () => groups.reduce((s, g) => s + g.total_usd_k, 0),
    [groups],
  );
  const partitionError = useMemo(
    () => (snap ? assertPositionsPartition(snap) : null),
    [snap],
  );

  // Per-account table sorting (applies to every account table). Click a
  // sortable header to sort; click again to flip direction.
  type SortKey = "symbol" | "sleeve" | "value" | "alloc" | "verdict";
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  // Page-level exclude-NVDA toggle — drives the composition donuts AND the
  // allocation-vs-target card so the whole view reads without NVDA's ~61%
  // concentration flattening it. Default on (per Ariel).
  const [excludeNvda, setExcludeNvda] = useState(true);
  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "value" || key === "alloc" ? "desc" : "asc");
    }
  }
  // Verdict ranked by actionability (BUY/ADD → SELL); unrated sorts last.
  const VERDICT_ORDER: Record<string, number> = {
    BUY: 0, ADD: 1, HOLD: 2, TRIM: 3, SELL: 4,
  };
  function sleeveOf(p: PortfolioPosition): string {
    return (p.sleeve || p.type_label || p.asset_type || "").trim();
  }
  /** Table-only short labels — hover/API keep the canonical full string. */
  function sleeveTableLabel(full: string): string {
    if (full === "Global quality growth (ex-NVDA-dense)") {
      return "Global quality growth";
    }
    if (full === "Cash & T-bills (incl. ILS tranche)") {
      return "Cash & T-bills";
    }
    return full;
  }
  function allocPct(p: PortfolioPosition): number | null {
    if (p.usd_value_k == null || liquidTotalK <= 0) return null;
    return (100 * p.usd_value_k) / liquidTotalK;
  }
  function sortPositions(positions: PortfolioPosition[]): PortfolioPosition[] {
    if (!sortKey) return positions;
    const dir = sortDir === "asc" ? 1 : -1;
    const key = (p: PortfolioPosition): string | number => {
      if (sortKey === "symbol") return (p.symbol || p.details || "").toLowerCase();
      if (sortKey === "sleeve") return sleeveOf(p).toLowerCase();
      if (sortKey === "value") return p.usd_value_k ?? -Infinity;
      if (sortKey === "alloc") return allocPct(p) ?? -Infinity;
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

  async function runHoldingsReviewNow() {
    setHoldingsReviewBusy(true);
    setHoldingsReviewMsg(null);
    try {
      const resp = await api.jobs.runNow("holdings_review");
      setHoldingsReviewMsg(
        `Holdings review queued (run #${resp.job_run_id}). Refresh in a minute.`,
      );
      // Soft refresh — verdicts may still be stale until the job finishes.
      window.setTimeout(() => refreshTheses(), 5_000);
    } catch (e: unknown) {
      setHoldingsReviewMsg(
        e instanceof Error ? e.message : "Could not queue holdings review",
      );
    } finally {
      setHoldingsReviewBusy(false);
    }
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

      {snap?.book_degraded && (
        <div
          className="rounded-md border border-rose-400/40 bg-rose-400/10 p-3"
          data-testid="book-degraded-banner"
        >
          <p className="text-sm font-medium text-rose-200">
            Valuation unavailable — total book degraded
          </p>
          {snap.degrade_reason && (
            <p className="mt-1 text-xs font-mono text-rose-100/90">
              {snap.degrade_reason}
            </p>
          )}
        </div>
      )}

      {(snap?.accounts_covered?.length || snap?.accounts_carried?.length) ? (
        <div
          className="rounded-md border border-sky-400/30 bg-sky-400/10 p-3"
          data-testid="accounts-coverage-banner"
        >
          <p className="text-sm font-medium text-sky-200">
            Snapshot account coverage
          </p>
          <p className="mt-1 text-xs font-mono text-sky-100/90">
            covered: {(snap.accounts_covered ?? []).join(", ") || "—"}
            {(snap.accounts_carried?.length ?? 0) > 0
              ? ` · carried forward: ${snap.accounts_carried!.join(", ")}`
              : ""}
          </p>
        </div>
      ) : null}

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

      {/* Fail-loud: held symbols missing from the §20.4 instrument reference.
          Their Type / sector / ESTATE-SAFETY are un-curated — a US-domiciled
          holding would otherwise be silently estate-unflagged. Surfaced loudly
          so the team classifies it rather than the system mis-typing it. */}
      {snap?.classification_warnings && snap.classification_warnings.length > 0 && (
        <div className="rounded-md border border-amber-400/40 bg-amber-400/10 p-3">
          <p className="text-sm font-medium text-amber-300">
            ⚠ {snap.classification_warnings.length} holding
            {snap.classification_warnings.length > 1 ? "s are" : " is"} not in the
            instrument reference — Type & estate-safety un-curated:
          </p>
          <p className="mt-1 text-xs font-mono text-amber-200/90">
            {snap.classification_warnings.join(", ")}
          </p>
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

      {/* Wealth dashboard — top-of-page retirement projection + 6 stat
         cards. Independent of the portfolio snapshot fetch above; renders
         on its own loading/error states. See
         argosy/services/wealth_dashboard.py for the aggregated payload. */}
      {/* Page-level exclude-NVDA toggle — applies to the composition donuts
         and the allocation-vs-target card below. */}
      <label className="flex items-center gap-2 text-sm text-muted-foreground cursor-pointer select-none self-start">
        <input
          type="checkbox"
          checked={excludeNvda}
          onChange={(e) => setExcludeNvda(e.target.checked)}
          className="accent-primary"
        />
        Exclude NVDA from charts, allocation &amp; estate exposure
        <span className="text-xs text-muted-foreground/70">
          (its ~61% RSU concentration otherwise dominates every view)
        </span>
      </label>

      <WealthDashboard userId={USER_ID} excludeNvda={excludeNvda} />

      {partitionError && (
        <p className="text-xs text-destructive" role="alert">
          Portfolio partition invariant failed: {partitionError}
        </p>
      )}

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

      <div className="flex flex-wrap items-center gap-3 self-start">
        <button
          type="button"
          disabled={holdingsReviewBusy}
          onClick={() => void runHoldingsReviewNow()}
          className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-secondary/50 disabled:opacity-50"
        >
          {holdingsReviewBusy ? "Queuing…" : "Run holdings review now"}
        </button>
        {holdingsReviewMsg && (
          <span className="text-xs text-muted-foreground">{holdingsReviewMsg}</span>
        )}
      </div>

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
                    onClick={() => toggleSort("sleeve")}
                  >
                    Sleeve{sortKey === "sleeve" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                  </th>
                  <th className="py-2">Estate</th>
                  <th className="py-2 text-right">Shares</th>
                  <th className="py-2 text-right">Price</th>
                  <th
                    className="py-2 text-right cursor-pointer hover:text-foreground"
                    onClick={() => toggleSort("value")}
                  >
                    K USD{sortKey === "value" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                  </th>
                  <th
                    className="py-2 text-right cursor-pointer hover:text-foreground"
                    onClick={() => toggleSort("alloc")}
                    title="% of liquid portfolio"
                  >
                    Alloc %{sortKey === "alloc" ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
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
                  const pct = allocPct(p);
                  return (
                    <tr
                      key={`${p.location}-${p.symbol || p.details}-${i}`}
                      className="border-b border-border/40"
                    >
                      <td className="py-1.5">
                        <HoldingHoverCard position={p} thesis={thesis}>
                          <div className="cursor-help underline decoration-dotted decoration-muted-foreground/40 underline-offset-2">
                            {symbolLabel}
                          </div>
                          {!isCash && p.name && (
                            <div className="text-[11px] text-muted-foreground/70 font-normal">
                              {p.name}
                            </div>
                          )}
                        </HoldingHoverCard>
                      </td>
                      <td
                        className="py-1.5 text-muted-foreground"
                        title={sleeveOf(p) || undefined}
                      >
                        {sleeveTableLabel(sleeveOf(p)) || "—"}
                        {sleeveOf(p) === "Unmapped — needs classification" && (
                          <div className="text-[10px] text-amber-400 font-normal">
                            needs classification
                          </div>
                        )}
                      </td>
                      <td className="py-1.5">
                        {p.classified === false ? (
                          <span
                            className="text-[10px] text-amber-400"
                            title="Not in the instrument reference — Type & estate-safety are un-curated. The team needs to classify this holding."
                          >
                            ⚠ unclassified
                          </span>
                        ) : p.estate_safe === null ? (
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
                            ⚠ US
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
                      <td
                        className="py-1.5 text-right tabular-nums text-muted-foreground"
                        title="% of liquid portfolio"
                      >
                        {pct != null ? `${pct.toFixed(1)}%` : "—"}
                      </td>
                      <td className="py-1.5 text-right">
                        {thesis ? (
                          <VerdictHoverCard thesis={thesis} />
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
      <AllocationBreakdownCard userId={USER_ID} excludeNvda={excludeNvda} />

      <InstrumentClassMapCard userId={USER_ID} />
    </main>
  );
}
