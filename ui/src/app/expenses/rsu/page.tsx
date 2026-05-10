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

// Small wire-icon glyph for העברת כספים rows.
function WireIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      className={cn("inline-block shrink-0", className)}
      aria-hidden="true"
    >
      <path
        fill="currentColor"
        d="M2 8h8.586l-2.293-2.293 1.414-1.414L14 8.586V9.414L9.707 13.707 8.293 12.293 10.586 10H2V8Z"
      />
    </svg>
  );
}

function NoiseIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      className={cn("inline-block shrink-0", className)}
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <circle cx="8" cy="8" r="2" fill="currentColor" />
    </svg>
  );
}

const WIRE_HEBREW = "העברת כספים";

function isWire(c: RsuLeumiCredit): boolean {
  return c.merchant_raw.includes(WIRE_HEBREW);
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

interface DisbRowProps {
  disb: RsuDisbursement;
  index: number;
  hovered: number | null;
  setHovered: (i: number | null) => void;
  highlightCreditTxId: number | null;
}

function DisbursementRow({
  disb,
  index,
  hovered,
  setHovered,
  highlightCreditTxId,
}: DisbRowProps) {
  const matched = disb.matched_leumi_credit_id !== null;
  const isHovered = hovered === index;
  const isMatchHighlight =
    highlightCreditTxId !== null &&
    disb.matched_leumi_credit_id === highlightCreditTxId;
  return (
    <div
      onMouseEnter={() => setHovered(index)}
      onMouseLeave={() => setHovered(null)}
      className={cn(
        "rounded-md border px-3 py-2 text-sm flex items-center justify-between gap-3 transition-colors",
        matched
          ? "border-emerald-400/60 bg-emerald-50/40 dark:bg-emerald-900/10"
          : "border-rose-400/60 bg-rose-50/40 dark:bg-rose-900/10",
        (isHovered || isMatchHighlight) && "ring-2 ring-sky-400/60",
      )}
    >
      <div className="flex items-center gap-3 min-w-0">
        <span className="font-mono text-xs text-muted-foreground">{disb.date}</span>
        <span className="font-mono font-medium">{fmtUSD(disb.amount_usd)}</span>
      </div>
      <div className="flex items-center gap-2">
        {matched ? (
          <Badge variant="success" className="text-[10px]">
            ✓ paired
            {disb.days_diff !== null && (
              <span className="opacity-80 ml-1">+{disb.days_diff}d</span>
            )}
          </Badge>
        ) : (
          <Badge variant="error" className="text-[10px]">✗ no match</Badge>
        )}
      </div>
    </div>
  );
}

interface CreditRowProps {
  credit: RsuLeumiCredit;
  highlight: boolean;
  matched: boolean;
}

function CreditRow({ credit, highlight, matched }: CreditRowProps) {
  const wire = isWire(credit);
  return (
    <div
      className={cn(
        "rounded-md border px-3 py-2 text-sm flex items-center justify-between gap-3 transition-colors",
        matched
          ? "border-emerald-400/60 bg-emerald-50/40 dark:bg-emerald-900/10"
          : "border-border bg-background",
        highlight && "ring-2 ring-sky-400/60",
      )}
    >
      <div className="flex items-center gap-3 min-w-0">
        <span className="font-mono text-xs text-muted-foreground">{credit.date}</span>
        <span className="font-mono font-medium">{fmtUSD(credit.amount_usd)}</span>
        <span
          className={cn(
            "shrink-0",
            wire ? "text-sky-600 dark:text-sky-400" : "text-muted-foreground",
          )}
          title={wire ? "Wire transfer" : "Other (dividend / interest)"}
        >
          {wire ? <WireIcon /> : <NoiseIcon />}
        </span>
        <span
          dir="rtl"
          className="text-xs text-muted-foreground truncate max-w-[14rem]"
          title={credit.merchant_raw}
        >
          {credit.merchant_raw}
        </span>
        {credit.reference && (
          <span className="font-mono text-[10px] text-muted-foreground">
            ref {credit.reference}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        {matched ? (
          <Badge variant="success" className="text-[10px]">✓ paired</Badge>
        ) : (
          <Badge variant="outline" className="text-[10px]">un-paired</Badge>
        )}
      </div>
    </div>
  );
}

export default function RsuPage() {
  const [data, setData] = useState<RsuReconciliationResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hoveredDisb, setHoveredDisb] = useState<number | null>(null);
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

  // Compute "nearby Leumi credit" highlight target on disbursement hover:
  // a credit within 14 days and within 50% of amount.
  let highlightCreditTxId: number | null = null;
  let highlightDisbCreditTxId: number | null = null;
  if (hoveredDisb !== null) {
    const disb = data.disbursements[hoveredDisb];
    if (disb) {
      // If matched, highlight the actual matched credit.
      if (disb.matched_leumi_credit_id !== null) {
        highlightCreditTxId = disb.matched_leumi_credit_id;
      } else {
        // Find the nearest unmatched credit within 14 days and 50% amount.
        const dDate = new Date(disb.date).getTime();
        let best: { txId: number; score: number } | null = null;
        for (const c of data.leumi_credits) {
          if (c.matched_disbursement_index !== null) continue;
          const cDate = new Date(c.date).getTime();
          const days = Math.abs((cDate - dDate) / 86_400_000);
          if (days > 14) continue;
          const amtRatio = Math.abs(c.amount_usd - disb.amount_usd) / disb.amount_usd;
          if (amtRatio > 0.5) continue;
          const score = days + amtRatio * 30; // crude composite
          if (best === null || score < best.score) {
            best = { txId: c.tx_id, score };
          }
        }
        if (best) highlightDisbCreditTxId = best.txId;
      }
    }
  }

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
              {s.leumi_credits_count} credit{s.leumi_credits_count !== 1 && "s"}
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

        {/* Right: Disbursements + Leumi credits */}
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <div className="text-sm font-semibold text-muted-foreground px-1">
              Schwab forced disbursements
            </div>
            {data.disbursements.length === 0 ? (
              <Card>
                <CardContent className="py-6 text-center text-muted-foreground text-sm">
                  No disbursements found.
                </CardContent>
              </Card>
            ) : (
              <div className="flex flex-col gap-1.5">
                {data.disbursements.map((d, i) => (
                  <DisbursementRow
                    key={i}
                    index={i}
                    disb={d}
                    hovered={hoveredDisb}
                    setHovered={setHoveredDisb}
                    highlightCreditTxId={highlightCreditTxId}
                  />
                ))}
              </div>
            )}
          </div>

          <div className="flex flex-col gap-2">
            <div className="text-sm font-semibold text-muted-foreground px-1">
              Leumi USD credits (± 30 days of disbursements)
            </div>
            {data.leumi_credits.length === 0 ? (
              <Card>
                <CardContent className="py-6 text-center text-muted-foreground text-sm">
                  No Leumi USD credits in the disbursement window.
                </CardContent>
              </Card>
            ) : (
              <div className="flex flex-col gap-1.5">
                {data.leumi_credits.map((c) => {
                  const matched = c.matched_disbursement_index !== null;
                  const highlight =
                    (highlightCreditTxId !== null &&
                      c.tx_id === highlightCreditTxId) ||
                    (highlightDisbCreditTxId !== null &&
                      c.tx_id === highlightDisbCreditTxId);
                  return (
                    <CreditRow
                      key={c.tx_id}
                      credit={c}
                      matched={matched}
                      highlight={highlight}
                    />
                  );
                })}
              </div>
            )}
          </div>
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
