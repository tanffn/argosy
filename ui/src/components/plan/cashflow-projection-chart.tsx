"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  type TooltipContentProps,
  XAxis,
  YAxis,
} from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type CashflowProjectionResponse } from "@/lib/api";

interface CashflowProjectionChartProps {
  userId: string;
}

interface ChartRow {
  months_out: number;
  age_years: number;
  date: string;
  portfolio_base: number;
  portfolio_band: [number, number]; // [bear, bull] for the area
  pension_annuity: number;
  total_income: number; // portfolio_base + pension_annuity
  expenses: number;
}

function fmtUsd(v: unknown): string {
  if (Array.isArray(v)) return v.map((x) => fmtUsd(x)).join(" – ");
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

function fmtSignedUsd(n: number): string {
  const sign = n >= 0 ? "+" : "−";
  return `${sign}${fmtUsd(Math.abs(n))}`;
}

export function CashflowProjectionChart({ userId }: CashflowProjectionChartProps) {
  const [data, setData] = useState<CashflowProjectionResponse | null>(null);
  const [retirementAge, setRetirementAge] = useState<number>(49);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Overlay toggles
  const [showBand, setShowBand] = useState(true);
  const [showAnnuity, setShowAnnuity] = useState(true);
  const [showLumpMarker, setShowLumpMarker] = useState(true);
  const [showRetireReady, setShowRetireReady] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- justified: retirement-age driven fetch; toggling loading/error inside the effect is the whole point
    setLoading(true);
    setError(null);
    api
      .planDraftCashflowProjection(userId, 30, retirementAge)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId, retirementAge]);

  const rows = useMemo<ChartRow[]>(() => {
    if (!data) return [];
    return data.series.map((p) => ({
      months_out: p.months_out,
      age_years: p.age_years,
      date: p.date,
      portfolio_base: p.portfolio_income_base_monthly_usd,
      portfolio_band: [
        p.portfolio_income_bear_monthly_usd,
        p.portfolio_income_bull_monthly_usd,
      ],
      pension_annuity: p.pension_annuity_monthly_usd,
      total_income:
        p.portfolio_income_base_monthly_usd + p.pension_annuity_monthly_usd,
      expenses: p.expenses_monthly_usd,
    }));
  }, [data]);

  const lumpAge = data?.assumptions.lump_pension_age ?? 60;
  const annuityAge = data?.assumptions.annuity_age ?? 67;
  const inflationAnnual = data?.assumptions.inflation_annual ?? 0.025;
  const realReturn = data?.assumptions.real_return_annual ?? 0.055;

  const renderTooltip = (tp: TooltipContentProps) => {
    if (!tp.active || !tp.payload || tp.payload.length === 0) return null;
    const row = tp.payload[0]?.payload as ChartRow | undefined;
    if (!row) return null;
    const delta = row.total_income - row.expenses;
    return (
      <div className="rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-sm">
        <div className="font-mono text-[10px] text-muted-foreground">
          age {row.age_years.toFixed(1)} · {row.date}
        </div>
        <div className="mt-1 grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5">
          <span className="text-muted-foreground">portfolio income (base)</span>
          <span className="font-mono">{fmtUsd(row.portfolio_base)}/mo</span>
          {showAnnuity && (
            <>
              <span className="text-muted-foreground">pension annuity</span>
              <span className="font-mono">{fmtUsd(row.pension_annuity)}/mo</span>
            </>
          )}
          <span className="font-medium">total income</span>
          <span className="font-mono font-medium">{fmtUsd(row.total_income)}/mo</span>
          <span className="text-muted-foreground">expenses (inflated)</span>
          <span className="font-mono">{fmtUsd(row.expenses)}/mo</span>
          <span className={delta >= 0 ? "text-success font-medium" : "text-error font-medium"}>
            {delta >= 0 ? "surplus" : "shortfall"}
          </span>
          <span
            className={`font-mono font-medium ${
              delta >= 0 ? "text-success" : "text-error"
            }`}
          >
            {fmtSignedUsd(delta)}/mo
          </span>
        </div>
      </div>
    );
  };

  if (loading && !data) {
    return (
      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle className="text-base">Monthly cashflow projection</CardTitle>
          <CardDescription>Loading…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (error || !data) {
    return (
      <Card className="lg:col-span-2">
        <CardHeader>
          <CardTitle className="text-base">Monthly cashflow projection</CardTitle>
          <CardDescription>{error ?? "No projection available."}</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const todayPortfolioIncome = rows[0]?.portfolio_base ?? 0;
  const todayExpenses = rows[0]?.expenses ?? 0;

  // Tick generation: integer ages every 5 years + key ages 60 + 67.
  const xTicks = (() => {
    if (rows.length === 0) return [];
    const minAge = Math.floor(rows[0].age_years);
    const maxAge = Math.ceil(rows[rows.length - 1].age_years);
    const out: number[] = [];
    for (let a = minAge; a <= maxAge; a += 5) out.push(a);
    if (!out.includes(lumpAge)) out.push(lumpAge);
    if (!out.includes(annuityAge)) out.push(annuityAge);
    return out.sort((a, b) => a - b);
  })();

  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardTitle className="text-base">
          Monthly cashflow projection · 30y
        </CardTitle>
        <CardDescription>
          When does projected monthly income cover expenses? Today:
          portfolio income{" "}
          <span className="font-mono">{fmtUsd(todayPortfolioIncome)}</span>/mo ·
          expenses <span className="font-mono">{fmtUsd(todayExpenses)}</span>/mo.{" "}
          {data.retire_ready_age != null ? (
            <>
              <span className="text-success font-medium">
                Retire-ready at age {data.retire_ready_age.toFixed(1)}
              </span>{" "}
              (assumed retirement age:{" "}
              <span className="font-mono">{retirementAge}</span>).
            </>
          ) : (
            <span className="text-error font-medium">
              No crossing in 30y at retirement age {retirementAge}.
            </span>
          )}
          <br />
          <span className="text-[10px] font-mono opacity-70">
            Real-return drawdown (mu={data.assumptions.mu_nominal_annual},
            inflation={inflationAnnual}, real={realReturn.toFixed(3)}). Pension
            annuity locks at {annuityAge} via mekadem={data.assumptions.mekadem}
            ; annuity inflated nominally after lock. Lump unlock at {lumpAge}.
          </span>
        </CardDescription>
        <div className="mt-3 flex flex-wrap items-center gap-4 text-xs">
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground">retirement age</span>
            <input
              type="range"
              min={Math.max(30, Math.floor(data.today_age_years))}
              max={70}
              step={1}
              value={retirementAge}
              onChange={(e) => setRetirementAge(Number(e.target.value))}
              className="w-40"
              aria-label="Retirement age"
            />
            <span className="font-mono w-8 text-right">{retirementAge}</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showBand}
              onChange={(e) => setShowBand(e.target.checked)}
            />
            <span>±1σ band</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showAnnuity}
              onChange={(e) => setShowAnnuity(e.target.checked)}
            />
            <span>pension annuity @ {annuityAge}</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showLumpMarker}
              onChange={(e) => setShowLumpMarker(e.target.checked)}
            />
            <span>lump marker @ {lumpAge}</span>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showRetireReady}
              onChange={(e) => setShowRetireReady(e.target.checked)}
            />
            <span>retire-ready marker</span>
          </label>
        </div>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={380}>
          <ComposedChart
            data={rows}
            margin={{ top: 8, right: 16, bottom: 4, left: 0 }}
          >
            <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
            <XAxis
              dataKey="age_years"
              fontSize={11}
              tickFormatter={(v: number) => `${Math.round(v)}`}
              domain={["dataMin", "dataMax"]}
              type="number"
              ticks={xTicks}
            />
            <YAxis fontSize={10} tickFormatter={(v) => fmtUsd(v)} width={64} />
            <Tooltip content={renderTooltip} />

            {showBand && (
              <Area
                type="monotone"
                dataKey="portfolio_band"
                stroke="none"
                fill="#6366f1"
                fillOpacity={0.15}
                isAnimationActive={false}
                name="±1σ portfolio band"
              />
            )}
            <Line
              type="monotone"
              dataKey="portfolio_base"
              stroke="#6366f1"
              strokeWidth={2.5}
              dot={false}
              isAnimationActive={false}
              name="portfolio income (base)"
            />
            {showAnnuity && (
              <Line
                type="monotone"
                dataKey="pension_annuity"
                stroke="#10b981"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
                name="pension annuity"
              />
            )}
            <Line
              type="monotone"
              dataKey="total_income"
              stroke="#f59e0b"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              name="total income"
            />
            <Line
              type="monotone"
              dataKey="expenses"
              stroke="#f43f5e"
              strokeWidth={1.5}
              strokeDasharray="4 4"
              dot={false}
              isAnimationActive={false}
              name="expenses (inflating)"
            />
            {showLumpMarker && (
              <ReferenceLine
                x={lumpAge}
                stroke="#a3a3a3"
                strokeDasharray="3 3"
                label={{
                  value: `lump @ ${lumpAge}`,
                  position: "top",
                  fill: "#a3a3a3",
                  fontSize: 10,
                }}
              />
            )}
            {showAnnuity && (
              <ReferenceLine
                x={annuityAge}
                stroke="#10b981"
                strokeDasharray="3 3"
                label={{
                  value: `annuity @ ${annuityAge}`,
                  position: "top",
                  fill: "#10b981",
                  fontSize: 10,
                }}
              />
            )}
            {showRetireReady && data.retire_ready_age != null && (
              <ReferenceLine
                x={data.retire_ready_age}
                stroke="#f59e0b"
                strokeWidth={2}
                label={{
                  value: `retire-ready ${data.retire_ready_age.toFixed(1)}`,
                  position: "insideTopRight",
                  fill: "#f59e0b",
                  fontSize: 11,
                  fontWeight: 600,
                }}
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
