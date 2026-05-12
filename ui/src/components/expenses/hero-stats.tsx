"use client";

import type { DashboardOverview, DashboardMonthly } from "@/lib/expenses/api";

type YearlyProps = {
  mode: "yearly";
  overview: DashboardOverview;
};
type MonthlyProps = {
  mode: "monthly";
  data: DashboardMonthly;
};
type Props = YearlyProps | MonthlyProps;

function fmt(n: number) {
  return `₪${Math.round(n).toLocaleString("en-IL")}`;
}

function pct(n: number | null) {
  if (n === null) return null;
  const sign = n >= 0 ? "+" : "";
  return `${sign}${Math.round(n * 100)}%`;
}

function DeltaPill({ delta }: { delta: number | null }) {
  if (delta === null) return <span className="text-xs text-muted-foreground">—</span>;
  const positive = delta >= 0;
  return (
    <span
      className={`text-xs tabular-nums px-1.5 py-0.5 rounded-sm ${
        positive ? "text-emerald-700 bg-emerald-100" : "text-rose-700 bg-rose-100"
      }`}
    >
      {pct(delta)}
    </span>
  );
}

function StatTile({ label, value, mom, vs12, title }: {
  label: string; value: string; mom?: number | null; vs12?: number | null;
  title?: string;
}) {
  return (
    <div
      className="rounded-md border border-border p-3 flex flex-col gap-1"
      title={title}
    >
      <div className="text-xs uppercase text-muted-foreground">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value}</div>
      {(mom !== undefined || vs12 !== undefined) && (
        <div className="flex items-center gap-2 mt-1">
          {mom !== undefined && (
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              vs prior <DeltaPill delta={mom} />
            </span>
          )}
          {vs12 !== undefined && (
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              vs avg <DeltaPill delta={vs12} />
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export function HeroStats(props: Props) {
  if (props.mode === "yearly") {
    const y = props.overview.yearly_summary;
    return (
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        <StatTile label="Spent (yr)" value={fmt(y.yearly_spending_total_nis)} />
        <StatTile label="Income (yr)" value={fmt(y.yearly_income_total_nis)} />
        <StatTile label="Refunds (yr)" value={fmt(y.yearly_refunds_total_nis)} />
        <StatTile label="Avg/mo" value={fmt(y.avg_per_month_nis)} />
        <StatTile
          label="Sources"
          value={String(props.overview.sources_health.length)}
        />
      </div>
    );
  }
  const h = props.data.hero_stats;
  return (
    <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
      <StatTile
        label="Spent"
        value={fmt(h.spent.value_nis)}
        mom={h.spent.mom_delta_pct}
        vs12={h.spent.vs_trailing12_pct}
      />
      <StatTile
        label="Income"
        value={fmt(h.income.value_nis)}
        mom={h.income.mom_delta_pct}
        vs12={h.income.vs_trailing12_pct}
      />
      <StatTile
        label="Refunds"
        value={fmt(h.refunds.value_nis)}
        mom={h.refunds.mom_delta_pct}
        vs12={h.refunds.vs_trailing12_pct}
      />
      <StatTile
        label="Reconciled"
        value={String(h.statements_reconciled)}
        title="Number of bank/credit-card statements covering this month whose parsed total matches what the bank declared (within ₪0.5). A data-quality indicator: how many source files came in clean."
      />
      <StatTile label="Anomalies" value={String(h.anomalies_count)} />
    </div>
  );
}
