"use client";

import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  type YearlySummary,
  type YearlyWindow,
} from "@/lib/expenses/api";
import { colorForSlug, formatNIS, formatPercent } from "@/lib/expenses/format";

interface YearlySummaryCardProps {
  data: YearlySummary;
  /** Toggling the window is a navigational concern (URL param + refetch),
   *  so we bubble the choice up rather than fetching here. */
  onWindowChange?: (next: YearlyWindow) => void;
}

const COLLAPSED_ROWS = 10;

function lastDayOfMonth(yyyymm: string): string {
  const [y, m] = yyyymm.split("-").map(Number);
  if (!y || !m) return yyyymm;
  // Last day = day 0 of next month
  const dt = new Date(y, m, 0);
  const dd = String(dt.getDate()).padStart(2, "0");
  return `${yyyymm}-${dd}`;
}

function firstDayOfMonth(yyyymm: string): string {
  return `${yyyymm}-01`;
}

/**
 * "Bottom line" card — full 12-month spending overview.
 *
 * Header carries a window toggle (Trailing 12 vs Calendar year). 4 big
 * numbers: spent / income / refunds / avg-per-month + trend arrow. Below:
 * paginated table of ALL spending categories (not just top 5) with NIS,
 * percent, transaction count, and a horizontal bar showing relative weight.
 * Each row links to the transactions page filtered to (category, window).
 */
export function YearlySummaryCard({
  data,
  onWindowChange,
}: YearlySummaryCardProps) {
  const [showAll, setShowAll] = useState(false);

  if (data.months_covered === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center justify-between gap-2">
            <span>Bottom line</span>
            <WindowToggle current={data.window} onChange={onWindowChange} />
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-sm text-muted-foreground py-4 text-center">
            Not enough data yet for a yearly rollup.
          </div>
        </CardContent>
      </Card>
    );
  }

  const trendLabel =
    data.current_vs_avg_pct === null
      ? null
      : data.current_vs_avg_pct >= 0
        ? `+${data.current_vs_avg_pct.toFixed(0)}% vs avg`
        : `${data.current_vs_avg_pct.toFixed(0)}% vs avg`;
  const trendColor =
    data.current_vs_avg_pct === null
      ? ""
      : data.current_vs_avg_pct > 5
        ? "text-rose-600"
        : data.current_vs_avg_pct < -5
          ? "text-emerald-600"
          : "text-muted-foreground";

  const startLabel = data.window_start_month;
  const endLabel = data.window_end_month;

  // Transactions deep-link target dates.
  const fromDate = data.window_start_month
    ? firstDayOfMonth(data.window_start_month)
    : null;
  const toDate = data.window_end_month
    ? lastDayOfMonth(data.window_end_month)
    : null;

  const cats = data.top_categories_12m;
  const visibleCats = showAll ? cats : cats.slice(0, COLLAPSED_ROWS);
  const maxCat = Math.max(1, ...cats.map((c) => c.total_nis));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-col">
            <span>Bottom line — {data.window_label || "last 12 months"}</span>
            {startLabel && endLabel && (
              <span className="text-xs font-normal text-muted-foreground">
                {startLabel} → {endLabel} · {data.months_covered} month
                {data.months_covered === 1 ? "" : "s"} of data
              </span>
            )}
          </div>
          <WindowToggle current={data.window} onChange={onWindowChange} />
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div>
            <div className="text-xs text-muted-foreground">Spent</div>
            <div className="text-2xl font-semibold tabular-nums">
              {formatNIS(data.yearly_spending_total_nis)}
            </div>
            <div className="text-xs text-muted-foreground">
              Outflow only — excludes salary, transfers, investments
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Income</div>
            <div className="text-2xl font-semibold tabular-nums text-emerald-600">
              {formatNIS(data.yearly_income_total_nis)}
            </div>
            <div className="text-xs text-muted-foreground">
              Salary, RSU, dividends
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Refunds</div>
            <div className="text-2xl font-semibold tabular-nums text-emerald-600">
              {formatNIS(data.yearly_refunds_total_nis)}
            </div>
            <div className="text-xs text-muted-foreground">
              Credits flagged tx_type=refund
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">
              Avg / month
            </div>
            <div className="text-2xl font-semibold tabular-nums">
              {formatNIS(data.avg_per_month_nis)}
            </div>
            {trendLabel && (
              <div className={`text-xs ${trendColor}`}>{trendLabel}</div>
            )}
          </div>
        </div>

        {cats.length > 0 && (
          <div className="mt-5">
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs text-muted-foreground">
                Where it goes — every spending category, sorted by total
              </div>
              <div className="text-xs text-muted-foreground tabular-nums">
                {cats.length} categor{cats.length === 1 ? "y" : "ies"}
              </div>
            </div>
            <ul className="flex flex-col gap-1.5">
              {visibleCats.map((c) => {
                const pctOfMax = (c.total_nis / maxCat) * 100;
                const href = buildTxLink(c.slug, fromDate, toDate);
                return (
                  <li key={c.slug}>
                    <Link
                      href={href}
                      className="block group hover:bg-accent/30 rounded px-1 -mx-1 py-1"
                    >
                      <div className="flex items-baseline gap-2 text-sm">
                        <span className="capitalize flex-1 min-w-0 truncate group-hover:underline">
                          {c.label_en}
                        </span>
                        <span className="tabular-nums text-muted-foreground text-xs w-14 text-right">
                          {c.transaction_count}{" "}
                          tx
                        </span>
                        <span className="tabular-nums text-muted-foreground text-xs w-12 text-right">
                          {formatPercent(c.percent)}
                        </span>
                        <span className="tabular-nums w-24 text-right">
                          {formatNIS(c.total_nis)}
                        </span>
                      </div>
                      <div className="h-1.5 w-full bg-secondary/40 rounded-full overflow-hidden mt-1">
                        <div
                          className="h-full rounded-full transition-all"
                          style={{
                            width: `${pctOfMax}%`,
                            backgroundColor: colorForSlug(c.slug),
                          }}
                        />
                      </div>
                    </Link>
                  </li>
                );
              })}
            </ul>
            {cats.length > COLLAPSED_ROWS && (
              <div className="mt-3 flex justify-center">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setShowAll(!showAll)}
                >
                  {showAll
                    ? "Show top 10"
                    : `Show all ${cats.length} categories`}
                </Button>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function buildTxLink(
  slug: string,
  fromDate: string | null,
  toDate: string | null,
): string {
  const qs = new URLSearchParams({ category: slug });
  if (fromDate) qs.set("from_date", fromDate);
  if (toDate) qs.set("to_date", toDate);
  return `/expenses/transactions?${qs.toString()}`;
}

interface WindowToggleProps {
  current: YearlyWindow;
  onChange?: (next: YearlyWindow) => void;
}

function WindowToggle({ current, onChange }: WindowToggleProps) {
  if (!onChange) return null;
  return (
    <div className="inline-flex rounded-md border border-border overflow-hidden text-xs font-normal">
      <button
        type="button"
        onClick={() => onChange("trailing_12")}
        className={
          current === "trailing_12"
            ? "bg-primary text-primary-foreground px-2 py-1"
            : "bg-background hover:bg-accent px-2 py-1"
        }
      >
        Trailing 12
      </button>
      <button
        type="button"
        onClick={() => onChange("calendar_year")}
        className={
          current === "calendar_year"
            ? "bg-primary text-primary-foreground px-2 py-1"
            : "bg-background hover:bg-accent px-2 py-1"
        }
      >
        Calendar year
      </button>
    </div>
  );
}
