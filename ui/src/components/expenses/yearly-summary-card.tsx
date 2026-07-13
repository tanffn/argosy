"use client";

import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  type CategorySpend,
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

/** Leaf slugs that roll up under a single "Vacation" parent in the
 *  Bottom-line category list. Hotels are lodging for trips; Vacation
 *  (other) is residual trip spend — both belong under one Vacation line
 *  with Hotels as a visible subcategory. Flights stay separate. */
const VACATION_CHILD_SLUGS = new Set([
  "travel.hotels",
  "travel.vacation_other",
]);

interface DisplayCategory {
  key: string;
  /** Leaf slug used for the transactions deep-link; null for the
   *  synthetic Vacation parent (children keep their own links). */
  slug: string | null;
  label_en: string;
  total_nis: number;
  transaction_count: number;
  percent: number;
  depth: 0 | 1;
}

function foldVacationCategories(
  cats: CategorySpend[],
  spendingTotal: number,
): DisplayCategory[] {
  const vacationKids = cats.filter((c) => VACATION_CHILD_SLUGS.has(c.slug));
  const rest = cats.filter((c) => !VACATION_CHILD_SLUGS.has(c.slug));
  const denom = spendingTotal || 1;

  const out: DisplayCategory[] = rest.map((c) => ({
    key: c.slug,
    slug: c.slug,
    label_en: c.label_en,
    total_nis: c.total_nis,
    transaction_count: c.transaction_count,
    percent: c.percent,
    depth: 0,
  }));

  if (vacationKids.length > 0) {
    const total = vacationKids.reduce((s, c) => s + c.total_nis, 0);
    const count = vacationKids.reduce((s, c) => s + c.transaction_count, 0);
    out.push({
      key: "vacation",
      slug: null,
      label_en: "Vacation",
      total_nis: total,
      transaction_count: count,
      percent: (total / denom) * 100,
      depth: 0,
    });
    for (const c of [...vacationKids].sort(
      (a, b) => b.total_nis - a.total_nis,
    )) {
      out.push({
        key: c.slug,
        slug: c.slug,
        label_en:
          c.slug === "travel.vacation_other" ? "Other" : c.label_en,
        total_nis: c.total_nis,
        transaction_count: c.transaction_count,
        percent: (c.total_nis / denom) * 100,
        depth: 1,
      });
    }
  }

  // Depth-0 by total desc; keep Vacation children immediately under parent.
  const parents = out.filter((r) => r.depth === 0);
  parents.sort((a, b) => b.total_nis - a.total_nis);
  const vacationChildren = out.filter((r) => r.depth === 1);
  const ordered: DisplayCategory[] = [];
  for (const p of parents) {
    ordered.push(p);
    if (p.key === "vacation") {
      ordered.push(...vacationChildren);
    }
  }
  return ordered;
}

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
  // Vacation parent row toggles its nested Hotels/Other children.
  const [vacationOpen, setVacationOpen] = useState(true);

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
        ? "text-error"
        : data.current_vs_avg_pct < -5
          ? "text-success"
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

  const displayCats = foldVacationCategories(
    data.top_categories_12m,
    data.yearly_spending_total_nis,
  );
  // Collapse counts top-level rows only so "Show top 10" isn't diluted
  // by Vacation's nested Hotels/Other lines.
  const topLevelKeys = new Set(
    displayCats.filter((c) => c.depth === 0).map((c) => c.key),
  );
  const topLevelOrdered = displayCats.filter((c) => c.depth === 0);
  const visibleTopKeys = new Set(
    (showAll ? topLevelOrdered : topLevelOrdered.slice(0, COLLAPSED_ROWS))
      .map((c) => c.key),
  );
  const visibleCats = displayCats.filter((c) => {
    if (c.depth === 0) return visibleTopKeys.has(c.key);
    // Show Vacation children only when the parent is visible AND expanded.
    return vacationOpen && visibleTopKeys.has("vacation");
  });
  const maxCat = Math.max(1, ...displayCats.filter((c) => c.depth === 0).map((c) => c.total_nis));
  const topLevelCount = topLevelKeys.size;

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
            <div className="text-2xl font-semibold font-mono tabular-nums">
              {formatNIS(data.yearly_spending_total_nis)}
            </div>
            <div className="text-xs text-muted-foreground">
              Outflow only — excludes salary, transfers, investments
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Income</div>
            <div className="text-2xl font-semibold font-mono tabular-nums text-success">
              {formatNIS(data.yearly_income_total_nis)}
            </div>
            <div className="text-xs text-muted-foreground">
              Salary, RSU, dividends
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Refunds</div>
            <div className="text-2xl font-semibold font-mono tabular-nums text-success">
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
            <div className="text-2xl font-semibold font-mono tabular-nums">
              {formatNIS(data.avg_per_month_nis)}
            </div>
            {trendLabel && (
              <div className={`text-xs font-mono tabular-nums ${trendColor}`}>{trendLabel}</div>
            )}
          </div>
        </div>

        {displayCats.length > 0 && (
          <div className="mt-5">
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs text-muted-foreground">
                Where it goes — every spending category, sorted by total
              </div>
              <div className="text-xs text-muted-foreground tabular-nums">
                {topLevelCount} categor{topLevelCount === 1 ? "y" : "ies"}
              </div>
            </div>
            <ul className="flex flex-col gap-1.5">
              {visibleCats.map((c) => {
                const pctOfMax = (c.total_nis / maxCat) * 100;
                const body = (
                  <>
                    <div className="flex items-baseline gap-2 text-sm">
                      <span
                        className={
                          "capitalize flex-1 min-w-0 truncate"
                          + (c.slug ? " group-hover:underline" : "")
                          + (c.depth === 1 ? " pl-4 text-muted-foreground" : "")
                          + (c.depth === 0 && c.key === "vacation" ? " font-medium" : "")
                        }
                      >
                        {c.key === "vacation"
                          ? `${vacationOpen ? "▾" : "▸"} ${c.label_en}`
                          : c.depth === 1
                            ? `↳ ${c.label_en}`
                            : c.label_en}
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
                          backgroundColor: colorForSlug(c.slug ?? c.key),
                          opacity: c.depth === 1 ? 0.7 : 1,
                        }}
                      />
                    </div>
                  </>
                );
                return (
                  <li key={c.key}>
                    {c.slug ? (
                      <Link
                        href={buildTxLink(c.slug, fromDate, toDate)}
                        className="block group hover:bg-accent/30 rounded px-1 -mx-1 py-1"
                      >
                        {body}
                      </Link>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setVacationOpen((v) => !v)}
                        title={
                          vacationOpen
                            ? "Collapse Vacation subcategories"
                            : "Expand Vacation subcategories"
                        }
                        className="block w-full text-left rounded px-1 -mx-1 py-1 hover:bg-accent/30 cursor-pointer"
                      >
                        {body}
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
            {topLevelCount > COLLAPSED_ROWS && (
              <div className="mt-3 flex justify-center">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setShowAll(!showAll)}
                >
                  {showAll
                    ? "Show top 10"
                    : `Show all ${topLevelCount} categories`}
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
