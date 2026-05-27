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

type Scenario = "bear" | "typical" | "bull";

interface ChartRow {
  months_out: number;
  age_years: number;
  date: string;
  portfolio_base: number;
  portfolio_bear: number;
  portfolio_bull: number;
  portfolio_band: [number, number]; // [bear, bull] for the area fill
  pension_annuity: number;
  total_income_base: number; // base + annuity
  total_income_bear: number; // bear + annuity
  total_income_bull: number; // bull + annuity
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
  const [scenario, setScenario] = useState<Scenario>("typical");
  const [taxRate, setTaxRate] = useState<number>(0.25);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Overlay toggles
  const [showBand, setShowBand] = useState(true);
  const [showAnnuity, setShowAnnuity] = useState(true);
  const [showLumpMarker, setShowLumpMarker] = useState(true);
  const [showRetireReady, setShowRetireReady] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- justified: retirement-age / tax-rate driven fetch; toggling loading/error inside the effect is the whole point
    setLoading(true);
    setError(null);
    api
      .planDraftCashflowProjection(userId, 30, retirementAge, taxRate)
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
  }, [userId, retirementAge, taxRate]);

  const rows = useMemo<ChartRow[]>(() => {
    if (!data) return [];
    return data.series.map((p) => ({
      months_out: p.months_out,
      age_years: p.age_years,
      date: p.date,
      portfolio_base: p.portfolio_income_base_monthly_usd,
      portfolio_bear: p.portfolio_income_bear_monthly_usd,
      portfolio_bull: p.portfolio_income_bull_monthly_usd,
      portfolio_band: [
        p.portfolio_income_bear_monthly_usd,
        p.portfolio_income_bull_monthly_usd,
      ],
      pension_annuity: p.pension_annuity_monthly_usd,
      total_income_base:
        p.portfolio_income_base_monthly_usd + p.pension_annuity_monthly_usd,
      total_income_bear:
        p.portfolio_income_bear_monthly_usd + p.pension_annuity_monthly_usd,
      total_income_bull:
        p.portfolio_income_bull_monthly_usd + p.pension_annuity_monthly_usd,
      expenses: p.expenses_monthly_usd,
    }));
  }, [data]);

  const lumpAge = data?.assumptions.lump_pension_age ?? 60;
  const annuityAge = data?.assumptions.annuity_age ?? 67;
  const inflationAnnual = data?.assumptions.inflation_annual ?? 0.025;
  const realReturn = data?.assumptions.real_return_annual ?? 0.055;
  const taxRateDisplay = data?.assumptions.tax_rate ?? taxRate;

  // Scenario-driven retire-ready age
  const retireReadyAge: number | null =
    scenario === "bear"
      ? (data?.retire_ready_age_bear ?? null)
      : scenario === "bull"
        ? (data?.retire_ready_age_bull ?? null)
        : (data?.retire_ready_age_base ?? null);

  const renderTooltip = (tp: TooltipContentProps) => {
    if (!tp.active || !tp.payload || tp.payload.length === 0) return null;
    const row = tp.payload[0]?.payload as ChartRow | undefined;
    if (!row) return null;
    const surplus =
      scenario === "bull"
        ? row.total_income_bull - row.expenses
        : scenario === "bear"
          ? row.total_income_bear - row.expenses
          : row.total_income_base - row.expenses;

    const isBear = scenario === "bear";
    const isTypical = scenario === "typical";
    const isBull = scenario === "bull";

    return (
      <div className="rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-sm">
        <div className="font-mono text-[10px] text-muted-foreground">
          age {row.age_years.toFixed(1)} · {row.date}
        </div>
        <div className="mt-1 grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5">
          <span className={isTypical ? "font-medium" : "text-muted-foreground"}>
            portfolio (base)
          </span>
          <span className={`font-mono${isTypical ? " font-medium" : ""}`}>
            {fmtUsd(row.portfolio_base)}/mo
          </span>
          <span className={isBear ? "font-medium text-rose-500" : "text-muted-foreground"}>
            portfolio (bear)
          </span>
          <span className={`font-mono${isBear ? " font-medium text-rose-500" : ""}`}>
            {fmtUsd(row.portfolio_bear)}/mo
          </span>
          <span className={isBull ? "font-medium text-emerald-500" : "text-muted-foreground"}>
            portfolio (bull)
          </span>
          <span className={`font-mono${isBull ? " font-medium text-emerald-500" : ""}`}>
            {fmtUsd(row.portfolio_bull)}/mo
          </span>
          {showAnnuity && (
            <>
              <span className="text-muted-foreground">pension annuity</span>
              <span className="font-mono">{fmtUsd(row.pension_annuity)}/mo</span>
            </>
          )}
          <span className="font-medium">
            total ({scenario})
          </span>
          <span className="font-mono font-medium">
            {fmtUsd(
              scenario === "bear"
                ? row.total_income_bear
                : scenario === "bull"
                  ? row.total_income_bull
                  : row.total_income_base,
            )}/mo
          </span>
          <span className="text-muted-foreground">expenses (inflated)</span>
          <span className="font-mono">{fmtUsd(row.expenses)}/mo</span>
          <span className={surplus >= 0 ? "text-success font-medium" : "text-error font-medium"}>
            {surplus >= 0 ? "surplus" : "shortfall"} (scenario: {scenario})
          </span>
          <span
            className={`font-mono font-medium ${
              surplus >= 0 ? "text-success" : "text-error"
            }`}
          >
            {fmtSignedUsd(surplus)}/mo
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
          {retireReadyAge != null ? (
            <>
              <span className="text-success font-medium">
                Retire-ready at age {retireReadyAge.toFixed(1)}
              </span>{" "}
              ({scenario} scenario · assumed retirement age:{" "}
              <span className="font-mono">{retirementAge}</span>).
            </>
          ) : (
            <span className="text-error font-medium">
              No crossing in 30y under {scenario} at retirement age {retirementAge}.
            </span>
          )}
          <br />
          <span className="text-[10px] font-mono opacity-70">
            Real-return drawdown (mu={data.assumptions.mu_nominal_annual},
            inflation={inflationAnnual}, real={realReturn.toFixed(3)}, tax={Math.round(taxRateDisplay * 100)}%).
            Portfolio income shown is NET of tax. Pension annuity not tax-adjusted.
            Pension annuity locks at {annuityAge} via mekadem={data.assumptions.mekadem}
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
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground">tax (cap gains)</span>
            <input
              type="range"
              min={0}
              max={0.5}
              step={0.05}
              value={taxRate}
              onChange={(e) => setTaxRate(Number(e.target.value))}
              className="w-32"
              aria-label="Tax rate (capital gains)"
            />
            <span className="font-mono w-8 text-right">{(taxRate * 100).toFixed(0)}%</span>
          </label>
          <fieldset className="flex items-center gap-2 border-0 p-0 m-0">
            <legend className="text-muted-foreground sr-only">Scenario</legend>
            <span className="text-muted-foreground">scenario</span>
            {(["bear", "typical", "bull"] as Scenario[]).map((s) => (
              <label key={s} className="flex items-center gap-1 cursor-pointer">
                <input
                  type="radio"
                  name="scenario"
                  value={s}
                  checked={scenario === s}
                  onChange={() => setScenario(s)}
                />
                <span
                  className={
                    s === "bear"
                      ? "text-rose-500"
                      : s === "bull"
                        ? "text-emerald-500"
                        : "text-indigo-400"
                  }
                >
                  {s}
                </span>
              </label>
            ))}
          </fieldset>
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
            {/* Bear portfolio line */}
            <Line
              type="monotone"
              dataKey="portfolio_bear"
              stroke="#f43f5e"
              strokeWidth={scenario === "bear" ? 2.5 : 1}
              strokeDasharray="6 3"
              dot={false}
              isAnimationActive={false}
              name="portfolio income (bear)"
            />
            {/* Bull portfolio line */}
            <Line
              type="monotone"
              dataKey="portfolio_bull"
              stroke="#10b981"
              strokeWidth={scenario === "bull" ? 2.5 : 1}
              strokeDasharray="6 3"
              dot={false}
              isAnimationActive={false}
              name="portfolio income (bull)"
            />
            {/* Base portfolio line */}
            <Line
              type="monotone"
              dataKey="portfolio_base"
              stroke="#6366f1"
              strokeWidth={scenario === "typical" ? 2.5 : 1.5}
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
            {/* Dynamic total income line for selected scenario */}
            <Line
              key="total_income"
              type="monotone"
              dataKey={`total_income_${scenario}`}
              stroke="#f59e0b"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              name={`total income (${scenario})`}
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
            {showRetireReady && retireReadyAge != null && (
              <ReferenceLine
                x={retireReadyAge}
                stroke="#f59e0b"
                strokeWidth={2}
                label={{
                  value: `retire-ready ${retireReadyAge.toFixed(1)} (${scenario})`,
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
