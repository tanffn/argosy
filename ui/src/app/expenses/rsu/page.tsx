"use client";

import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  expensesApi,
  type RsuDisbursement,
  type RsuLeumiCredit,
  type RsuReconciliationResponse,
  type RsuSale,
} from "@/lib/expenses/api";
import { cn } from "@/lib/utils";

const USER_ID = "ariel";

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function fmtUSD(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return USD.format(n);
}

interface SaleRowProps {
  sale: RsuSale;
}

function SaleCard({ sale }: SaleRowProps) {
  const [open, setOpen] = useState(false);
  return (
    <Card className="py-3 gap-2">
      <CardHeader className="pb-1 px-4">
        <CardTitle className="flex items-center gap-2 text-sm font-medium">
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            className="text-muted-foreground hover:text-foreground transition-colors"
            aria-expanded={open}
            aria-label={open ? "Collapse lots" : "Expand lots"}
          >
            <span className={cn("inline-block transition-transform", open && "rotate-90")}>
              ▶
            </span>
          </button>
          <span className="font-mono text-xs text-muted-foreground">{sale.date}</span>
          <span className="font-semibold">{sale.symbol}</span>
          <span className="text-muted-foreground">·</span>
          <span>{sale.quantity_shares.toLocaleString()} shares</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pt-0 text-sm">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-xs">
          <div>
            <span className="text-muted-foreground">gross</span>{" "}
            <span className="font-mono">{fmtUSD(sale.gross_usd)}</span>
          </div>
          <div>
            <span className="text-muted-foreground">fees</span>{" "}
            <span className="font-mono">{fmtUSD(sale.fees_usd)}</span>
          </div>
          <div>
            <span className="text-muted-foreground">taxes</span>{" "}
            <span className="font-mono">{fmtUSD(sale.total_taxes_usd)}</span>
          </div>
          <div>
            <span className="text-muted-foreground">net</span>{" "}
            <span className="font-mono font-medium">{fmtUSD(sale.net_usd)}</span>
          </div>
        </div>
        {open && sale.lots.length > 0 && (
          <div className="mt-2 border-l-2 border-border pl-3 space-y-1">
            {sale.lots.map((lot, i) => (
              <div key={i} className="text-xs grid grid-cols-1 md:grid-cols-2 gap-x-3 gap-y-0.5">
                <div className="flex items-center gap-2">
                  <span className="font-mono">
                    {lot.shares.toLocaleString()} sh @ {fmtUSD(lot.sale_price_usd)}
                  </span>
                  {lot.holding_period && (
                    <span
                      className={cn(
                        "rounded px-1.5 py-0 text-[10px] uppercase tracking-wider border",
                        lot.holding_period === "LONG TERM"
                          ? "border-emerald-300 text-emerald-700 bg-emerald-50 dark:bg-emerald-900/20 dark:text-emerald-300 dark:border-emerald-700"
                          : "border-amber-300 text-amber-700 bg-amber-50 dark:bg-amber-900/20 dark:text-amber-300 dark:border-amber-700",
                      )}
                    >
                      {lot.holding_period}
                    </span>
                  )}
                </div>
                <div className="text-muted-foreground space-x-3">
                  {lot.vest_date && <span>vest {lot.vest_date}</span>}
                  {lot.cost_basis_usd !== null && (
                    <span>basis {fmtUSD(lot.cost_basis_usd)}</span>
                  )}
                  {lot.realized_gain_usd !== null && (
                    <span>gain {fmtUSD(lot.realized_gain_usd)}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Paired rows: one row per matched-pair / Schwab-only / Leumi-only
// ---------------------------------------------------------------------------
type PairRow =
  | {
      kind: "matched";
      disb: RsuDisbursement;
      credit: RsuLeumiCredit;
      daysDiff: number;
      // Signed: positive = bank received less than Schwab sent (haircut).
      amountDiff: number;
      matchKind: "exact" | "haircut";
      haircutPct: number;
    }
  | { kind: "schwab_only"; disb: RsuDisbursement }
  | { kind: "leumi_only"; credit: RsuLeumiCredit };

function buildPairs(resp: RsuReconciliationResponse): PairRow[] {
  const matchedCreditIds = new Set(
    resp.disbursements
      .filter((d) => d.matched_leumi_credit_id !== null)
      .map((d) => d.matched_leumi_credit_id as number),
  );
  const rows: PairRow[] = [];
  // Matched pairs first, then Schwab-only, then Leumi-only — but we sort the
  // whole list by primary date desc at the end so all rows interleave by date.
  for (const d of resp.disbursements) {
    if (d.matched_leumi_credit_id !== null) {
      const c = resp.leumi_credits.find(
        (cr) => cr.tx_id === d.matched_leumi_credit_id,
      );
      if (c) {
        rows.push({
          kind: "matched",
          disb: d,
          credit: c,
          // The matcher always populates these on a paired disbursement.
          daysDiff: d.days_diff ?? 0,
          amountDiff: d.amount_diff_usd ?? 0,
          matchKind: d.match_kind ?? "exact",
          haircutPct: d.haircut_pct ?? 0,
        });
      }
    }
  }
  for (const d of resp.disbursements) {
    if (d.matched_leumi_credit_id === null) {
      rows.push({ kind: "schwab_only", disb: d });
    }
  }
  for (const c of resp.leumi_credits) {
    if (!matchedCreditIds.has(c.tx_id)) {
      rows.push({ kind: "leumi_only", credit: c });
    }
  }
  rows.sort((a, b) => {
    const da =
      a.kind === "leumi_only" ? a.credit.date : a.disb.date;
    const db =
      b.kind === "leumi_only" ? b.credit.date : b.disb.date;
    return db.localeCompare(da);
  });
  return rows;
}

function DisbCell({ disb }: { disb: RsuDisbursement }) {
  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="font-mono text-xs text-muted-foreground">
        {disb.date}
      </span>
      <span className="font-mono font-medium">
        {fmtUSD(disb.amount_usd)}
      </span>
    </div>
  );
}

function CreditCell({ credit }: { credit: RsuLeumiCredit }) {
  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="font-mono text-xs text-muted-foreground">
        {credit.date}
      </span>
      <span className="font-mono font-medium">
        {fmtUSD(credit.amount_usd)}
      </span>
      <span
        dir="rtl"
        className="text-xs text-muted-foreground truncate max-w-[10rem]"
        title={credit.merchant_raw}
      >
        {credit.merchant_raw}
      </span>
      {credit.reference && (
        <span className="font-mono text-[10px] text-muted-foreground shrink-0">
          ref {credit.reference}
        </span>
      )}
    </div>
  );
}

function PlaceholderCell({ label }: { label: string }) {
  return (
    <div className="rounded-md border border-dashed border-muted-foreground/30 px-2 py-1 text-xs text-muted-foreground italic">
      ?? {label}
    </div>
  );
}

function PairRowView({ row }: { row: PairRow }) {
  if (row.kind === "matched") {
    const isHaircut = row.matchKind === "haircut";
    // Show the absolute dollar shortfall with a leading minus to make the
    // "bank received less" direction visually obvious.
    const haircutAmountText = `−${fmtUSD(Math.abs(row.amountDiff))}`;
    return (
      <div
        className={cn(
          "grid grid-cols-[1fr_auto_1fr] items-center gap-3 rounded-md border px-3 py-2 text-sm",
          isHaircut
            ? "border-l-4 border-l-sky-500 border-sky-400/60 bg-sky-50/40 dark:bg-sky-900/10"
            : "border-l-4 border-l-emerald-500 border-emerald-400/60 bg-emerald-50/40 dark:bg-emerald-900/10",
        )}
      >
        <DisbCell disb={row.disb} />
        {isHaircut ? (
          <Badge
            className="text-[10px] justify-self-center border-transparent bg-sky-500 text-white"
            title="Likely IL capital-gains tax withholding (~28%)"
          >
            ≈ haircut
            <span className="opacity-80 ml-1">
              {row.daysDiff >= 0 ? "+" : ""}
              {row.daysDiff}d
            </span>
            <span className="opacity-80 ml-1">·</span>
            <span className="opacity-90 ml-1">
              {haircutAmountText} ({row.haircutPct.toFixed(1)}%)
            </span>
          </Badge>
        ) : (
          <Badge variant="success" className="text-[10px] justify-self-center">
            ✓ paired
            <span className="opacity-80 ml-1">
              {row.daysDiff >= 0 ? "+" : ""}
              {row.daysDiff}d
            </span>
            <span className="opacity-80 ml-1">·</span>
            <span className="opacity-80 ml-1">
              Δ {fmtUSD(row.amountDiff)}
            </span>
          </Badge>
        )}
        <CreditCell credit={row.credit} />
      </div>
    );
  }
  if (row.kind === "schwab_only") {
    return (
      <div
        className={cn(
          "grid grid-cols-[1fr_auto_1fr] items-center gap-3 rounded-md border px-3 py-2 text-sm",
          "border-l-4 border-l-rose-500 border-rose-400/60 bg-rose-50/40 dark:bg-rose-900/10",
        )}
      >
        <DisbCell disb={row.disb} />
        <Badge variant="error" className="text-[10px] justify-self-center">
          ✗ no match
        </Badge>
        <PlaceholderCell label="no Leumi wire received" />
      </div>
    );
  }
  // leumi_only
  return (
    <div
      className={cn(
        "grid grid-cols-[1fr_auto_1fr] items-center gap-3 rounded-md border px-3 py-2 text-sm",
        "border-r-4 border-r-rose-500 border-amber-400/60 bg-amber-50/40 dark:bg-amber-900/10",
      )}
    >
      <PlaceholderCell label="no Schwab disbursement" />
      <Badge
        className="text-[10px] justify-self-center border-transparent bg-amber-500 text-white"
      >
        ?? orphan
      </Badge>
      <CreditCell credit={row.credit} />
    </div>
  );
}

export default function RsuPage() {
  const [data, setData] = useState<RsuReconciliationResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Ticking this state forces a refetch from the "Refresh from disk" button.
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const d = await expensesApi.rsuReconciliation(USER_ID);
        if (!cancelled) setData(d);
      } catch (e) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  const fetchData = useCallback(() => {
    setRefreshKey((k) => k + 1);
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
  if (loading || !data) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground text-sm">
          Loading RSU reconciliation…
        </CardContent>
      </Card>
    );
  }

  const s = data.summary;
  const matchRateOK = s.disbursements_matched_count === s.disbursements_count;
  const unmatchedDisbCount = s.disbursements_count - s.disbursements_matched_count;

  const pairRows = buildPairs(data);

  return (
    <div className="flex flex-col gap-4">
      {/* Title row + refresh */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">RSU reconciliation</h2>
          <div className="text-sm text-muted-foreground">
            Schwab sales → forced disbursements → Leumi USD credits, side by side.
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={fetchData} disabled={loading}>
          Refresh from disk
        </Button>
      </div>

      {/* Warning */}
      {data.warning && (
        <Card className="border-amber-400/60 bg-amber-50/40 dark:bg-amber-900/10">
          <CardContent className="py-3 text-sm">
            <div className="font-medium mb-1">Schwab CSVs not loaded</div>
            <div className="text-muted-foreground">
              {data.warning}. Set{" "}
              <code className="px-1 py-0.5 bg-secondary rounded text-xs">
                ARGOSY_EXPENSE_SAMPLES_ROOT
              </code>{" "}
              or drop CSVs in{" "}
              <code className="px-1 py-0.5 bg-secondary rounded text-xs">
                &lt;root&gt;/&lt;year&gt;/Schwab/
              </code>
              .
            </div>
          </CardContent>
        </Card>
      )}

      {/* Hero row */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Sold
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold font-mono">
              {fmtUSD(s.sales_total_gross_usd)}
            </div>
            <div className="text-xs text-muted-foreground mt-1">
              {s.sales_count} sale{s.sales_count !== 1 && "s"}
              {data.sales.length > 0 && (
                <>
                  {" "}
                  ·{" "}
                  {data.sales
                    .reduce((acc, s2) => acc + s2.quantity_shares, 0)
                    .toLocaleString()}{" "}
                  shares
                </>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Wired out of Schwab
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold font-mono">
              {fmtUSD(s.disbursements_total_usd)}
            </div>
            <div className="text-xs text-muted-foreground mt-1 flex items-center gap-2">
              <span>
                {s.disbursements_count} disbursement
                {s.disbursements_count !== 1 && "s"}
              </span>
              {s.disbursements_count > 0 && (
                <Badge
                  variant={matchRateOK ? "success" : "error"}
                  className="text-[10px]"
                >
                  {matchRateOK ? "✓" : "✗"} {s.disbursements_matched_count}/
                  {s.disbursements_count} matched
                  {!matchRateOK && (
                    <span className="ml-1 opacity-80">
                      · {unmatchedDisbCount} unaccounted
                    </span>
                  )}
                </Badge>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Received in Leumi USD
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold font-mono">
              {fmtUSD(
                data.leumi_credits.reduce((a, c) => a + c.amount_usd, 0),
              )}
            </div>
            <div className="text-xs text-muted-foreground mt-1">
              {s.leumi_credits_count} wire{s.leumi_credits_count !== 1 && "s"}
              {" "}
              <span className="opacity-70">(העברת כספים only)</span>
              {s.leumi_credits_unmatched_count > 0 && (
                <>
                  {" "}
                  · {s.leumi_credits_unmatched_count} un-paired (
                  {fmtUSD(s.leumi_credits_unmatched_total_usd)})
                </>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Two-column side-by-side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Left: Sales */}
        <div className="flex flex-col gap-2">
          <div className="text-sm font-semibold text-muted-foreground px-1">
            Schwab sales (newest first)
          </div>
          {data.sales.length === 0 ? (
            <Card>
              <CardContent className="py-6 text-center text-muted-foreground text-sm">
                No sales loaded.
              </CardContent>
            </Card>
          ) : (
            data.sales.map((sale, i) => <SaleCard key={i} sale={sale} />)
          )}
        </div>

        {/* Right: Paired Schwab disbursement <-> Leumi wire credit */}
        <div className="flex flex-col gap-2">
          <div className="text-sm font-semibold text-muted-foreground px-1">
            Disbursements ↔ Leumi wires (paired view)
          </div>
          {pairRows.length === 0 ? (
            <Card>
              <CardContent className="py-6 text-center text-muted-foreground text-sm">
                Nothing to pair yet — no disbursements and no Leumi wires in
                the window.
              </CardContent>
            </Card>
          ) : (
            <>
              <div
                className="grid grid-cols-[1fr_auto_1fr] items-center gap-3 px-3 text-[10px] uppercase tracking-wider text-muted-foreground"
              >
                <span>Schwab disbursement</span>
                <span className="justify-self-center">match</span>
                <span>Leumi wire credit</span>
              </div>
              <div className="flex flex-col gap-1.5">
                {pairRows.map((row, i) => (
                  <PairRowView
                    key={
                      row.kind === "leumi_only"
                        ? `c-${row.credit.tx_id}`
                        : row.kind === "matched"
                          ? `m-${row.disb.date}-${row.credit.tx_id}`
                          : `d-${row.disb.date}-${i}`
                    }
                    row={row}
                  />
                ))}
              </div>
              <div className="text-[11px] text-muted-foreground px-1 pt-1 leading-relaxed">
                Pairs flagged with <span className="font-medium">≈</span> are
                soft-matched — Leumi credit is smaller than the Schwab
                disbursement by ~28% (consistent with Israeli capital-gains tax
                withholding). Tolerance: 60-105% of disbursement, ±14 days.
              </div>
            </>
          )}
        </div>
      </div>

      {/* Footer: schwab paths */}
      {data.schwab_csv_paths.length > 0 && (
        <div className="text-xs text-muted-foreground">
          Parsed {data.schwab_csv_paths.length} Schwab CSV
          {data.schwab_csv_paths.length !== 1 && "s"}:
          <ul className="mt-1 space-y-0.5">
            {data.schwab_csv_paths.map((p) => (
              <li key={p} className="font-mono break-all">
                {p}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
