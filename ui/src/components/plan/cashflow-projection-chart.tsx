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
import {
  api,
  type CashflowProjectionResponse,
  type MonteCarloProjectionResponse,
} from "@/lib/api";

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

function InfoIcon({ title }: { title: string }) {
  return (
    <span
      role="img"
      aria-label={title}
      title={title}
      className="inline-block ml-1 text-xs text-primary/80 cursor-help select-none hover:text-primary"
      style={{ verticalAlign: "baseline" }}
    >
      ⓘ
    </span>
  );
}

function formatPortfolioForPlaceholder(data: CashflowProjectionResponse): string {
  // We don't get the actual portfolio value back in the response — but we
  // can derive it from the t=0 base income: income = portfolio * real_return * (1-tax) / 12
  // → portfolio = income * 12 / real_return / (1-tax).
  const real = data.assumptions.real_return_annual;
  const tax = data.assumptions.tax_rate;
  const income = data.series[0]?.portfolio_income_base_monthly_usd ?? 0;
  if (real <= 0 || income <= 0 || tax >= 1) return "";
  const portfolio = (income * 12) / real / (1 - tax);
  return `$${(portfolio / 1_000_000).toFixed(2)}M`;
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
  const [muNominal, setMuNominal] = useState<number>(0.08);
  const [portfolioOverrideUsd, setPortfolioOverrideUsd] = useState<number | null>(null);
  const [sigmaAnnual, setSigmaAnnual] = useState<number>(0.18);
  const [lifestyleDriftAnnual, setLifestyleDriftAnnual] = useState<number>(0.0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Monte Carlo view state
  type View = "deterministic" | "monteCarlo";
  const [view, setView] = useState<View>("deterministic");
  const [mcData, setMcData] = useState<MonteCarloProjectionResponse | null>(null);
  const [mcLoading, setMcLoading] = useState(false);
  const [mcError, setMcError] = useState<string | null>(null);
  const [nPaths, setNPaths] = useState<number>(1000);

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
      .planDraftCashflowProjection(userId, 30, retirementAge, taxRate, muNominal, portfolioOverrideUsd, sigmaAnnual, lifestyleDriftAnnual)
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
  }, [userId, retirementAge, taxRate, muNominal, portfolioOverrideUsd, sigmaAnnual, lifestyleDriftAnnual]);

  useEffect(() => {
    if (view !== "monteCarlo") return;
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- justified: view / nPaths driven fetch; toggling loading/error inside the effect is the whole point
    setMcLoading(true);
    setMcError(null);
    api
      .planDraftCashflowMonteCarlo(userId, {
        years: 40,
        retirementAge,
        taxRate,
        muNominalAnnual: muNominal,
        sigmaAnnual,
        lifestyleDriftAnnual,
        portfolioValueUsdOverride: portfolioOverrideUsd,
        nPaths,
      })
      .then((d) => {
        if (!cancelled) setMcData(d);
      })
      .catch((e) => {
        if (!cancelled) setMcError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setMcLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [view, userId, retirementAge, taxRate, muNominal, sigmaAnnual, lifestyleDriftAnnual, portfolioOverrideUsd, nPaths]);

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

  // MC tick generation: integer ages every 5 years + key ages (lump/annuity).
  const xTicksMC = useMemo(() => {
    const series = mcData?.series ?? data?.series ?? [];
    if (series.length === 0) return [];
    const minAge = Math.floor(series[0].age_years);
    const maxAge = Math.ceil(series[series.length - 1].age_years);
    const lump = mcData?.assumptions.lump_pension_age ?? lumpAge;
    const annuity = mcData?.assumptions.annuity_age ?? annuityAge;
    const out: number[] = [];
    for (let a = minAge; a <= maxAge; a += 5) out.push(a);
    if (!out.includes(lump)) out.push(lump);
    if (!out.includes(annuity)) out.push(annuity);
    return out.sort((a, b) => a - b);
  }, [mcData, data, lumpAge, annuityAge]);

  // Scenario-driven retire-ready age (deterministic view only)
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

  const renderMonteCarloTooltip = (tp: TooltipContentProps) => {
    if (!tp.active || !tp.payload || tp.payload.length === 0) return null;
    const row = tp.payload[0]?.payload as {
      age_years: number;
      p10_p90_band: [number, number];
      p25_p75_band: [number, number];
      p50: number;
      fraction_solvent: number;
    } | undefined;
    if (!row) return null;
    return (
      <div className="rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-sm">
        <div className="font-mono text-[10px] text-muted-foreground">
          age {row.age_years.toFixed(1)}
        </div>
        <div className="mt-1 grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5">
          <span className="text-muted-foreground">P90 (best 10%)</span>
          <span className="font-mono">{fmtUsd(row.p10_p90_band[1])}</span>
          <span className="text-muted-foreground">P75</span>
          <span className="font-mono">{fmtUsd(row.p25_p75_band[1])}</span>
          <span className="font-medium">P50 (median)</span>
          <span className="font-mono font-medium">{fmtUsd(row.p50)}</span>
          <span className="text-muted-foreground">P25</span>
          <span className="font-mono">{fmtUsd(row.p25_p75_band[0])}</span>
          <span className="text-muted-foreground">P10 (worst 10%)</span>
          <span className="font-mono">{fmtUsd(row.p10_p90_band[0])}</span>
          <span className="border-t border-border/40 pt-1 text-muted-foreground">% paths solvent</span>
          <span className="border-t border-border/40 pt-1 font-mono">{row.fraction_solvent.toFixed(1)}%</span>
        </div>
      </div>
    );
  };

  const renderMonteCarlo = (mc: MonteCarloProjectionResponse) => {
    const mcRows = mc.series.map((p) => ({
      age_years: p.age_years,
      p10_p90_band: [p.portfolio_value_p10_usd, p.portfolio_value_p90_usd] as [number, number],
      p25_p75_band: [p.portfolio_value_p25_usd, p.portfolio_value_p75_usd] as [number, number],
      p50: p.portfolio_value_p50_usd,
      fraction_solvent: p.fraction_solvent * 100,
    }));
    const mcLumpAge = mc.assumptions.lump_pension_age;
    const mcAnnuityAge = mc.assumptions.annuity_age;
    return (
      <>
        <div className="mt-3 grid grid-cols-3 gap-3 text-sm">
          <div className="rounded-md border border-border/60 bg-muted/20 p-2 text-center">
            <div className="text-[10px] uppercase text-muted-foreground">P(broke before 75)</div>
            <div className={`font-mono text-lg font-medium ${mc.p_failure_before_age_75 > 0.10 ? "text-error" : "text-success"}`}>
              {(mc.p_failure_before_age_75 * 100).toFixed(1)}%
            </div>
          </div>
          <div className="rounded-md border border-border/60 bg-muted/20 p-2 text-center">
            <div className="text-[10px] uppercase text-muted-foreground">P(broke before 85)</div>
            <div className={`font-mono text-lg font-medium ${mc.p_failure_before_age_85 > 0.20 ? "text-error" : "text-success"}`}>
              {(mc.p_failure_before_age_85 * 100).toFixed(1)}%
            </div>
          </div>
          <div className="rounded-md border border-border/60 bg-muted/20 p-2 text-center">
            <div className="text-[10px] uppercase text-muted-foreground">P(broke before 95)</div>
            <div className={`font-mono text-lg font-medium ${mc.p_failure_before_age_95 > 0.30 ? "text-error" : "text-success"}`}>
              {(mc.p_failure_before_age_95 * 100).toFixed(1)}%
            </div>
          </div>
        </div>
        <ResponsiveContainer width="100%" height={400}>
          <ComposedChart data={mcRows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
            <XAxis
              dataKey="age_years"
              fontSize={11}
              tickFormatter={(v: number) => `${Math.round(v)}`}
              domain={["dataMin", "dataMax"]}
              type="number"
              ticks={xTicksMC}
            />
            <YAxis
              fontSize={10}
              tickFormatter={(v) => fmtUsd(v)}
              width={72}
              label={{ value: "$ portfolio", angle: -90, position: "insideLeft", style: { fontSize: 10, fill: "#999" } }}
            />
            <Tooltip content={renderMonteCarloTooltip} />
            <Area
              type="monotone"
              dataKey="p10_p90_band"
              stroke="none"
              fill="#6366f1"
              fillOpacity={0.10}
              isAnimationActive={false}
              name="P10-P90 band"
            />
            <Area
              type="monotone"
              dataKey="p25_p75_band"
              stroke="none"
              fill="#6366f1"
              fillOpacity={0.20}
              isAnimationActive={false}
              name="P25-P75 band"
            />
            <Line
              type="monotone"
              dataKey="p50"
              stroke="#6366f1"
              strokeWidth={2.5}
              dot={false}
              isAnimationActive={false}
              name="median path (P50)"
            />
            {showLumpMarker && (
              <ReferenceLine x={mcLumpAge} stroke="#a3a3a3" strokeDasharray="3 3" label={{ value: `lump @ ${mcLumpAge}`, position: "top", fill: "#a3a3a3", fontSize: 10 }} />
            )}
            {showAnnuity && (
              <ReferenceLine x={mcAnnuityAge} stroke="#10b981" strokeDasharray="3 3" label={{ value: `annuity @ ${mcAnnuityAge}`, position: "top", fill: "#10b981", fontSize: 10 }} />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </>
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
            Real-return drawdown (μ={data.assumptions.mu_nominal_annual}, σ={data.assumptions.sigma_annual},
            inflation={data.assumptions.inflation_annual}, real={data.assumptions.real_return_annual.toFixed(3)},
            tax={(data.assumptions.tax_rate*100).toFixed(0)}%
            {data.assumptions.lifestyle_drift_annual > 0 && (
              <>, expense growth={(data.assumptions.effective_expense_growth*100).toFixed(1)}%</>
            )}).
            Pension annuity locks at {annuityAge} via mekadem={data.assumptions.mekadem}. Lump unlock at {lumpAge}.
          </span>
          {portfolioOverrideUsd != null && (
            <span className="ml-2 text-amber-500 font-medium">
              [override active: ${(portfolioOverrideUsd / 1_000_000).toFixed(2)}M]
            </span>
          )}
        </CardDescription>
        <div className="mt-3 flex flex-wrap items-center gap-4 text-xs">
          {/* View toggle — must be first in the controls row */}
          <fieldset className="flex items-center gap-2">
            <legend className="sr-only">View</legend>
            <span className="text-muted-foreground">
              view
              <InfoIcon title={`Deterministic: shows the bear/typical/bull bands assuming returns equal their mean every month. Sharp lines, no probability info.\nMonte Carlo: simulates ${nPaths} random walks per scenario. Shows percentile bands (P10/P25/P50/P75/P90) and the probability of running out of money before key ages. Captures sequence-of-returns risk that the deterministic chart cannot show.`} />
            </span>
            {(["deterministic", "monteCarlo"] as View[]).map((v) => (
              <label key={v} className="flex items-center gap-1">
                <input
                  type="radio"
                  name="view"
                  value={v}
                  checked={view === v}
                  onChange={() => setView(v)}
                />
                <span className="font-mono text-xs">{v === "monteCarlo" ? "Monte Carlo" : "deterministic"}</span>
              </label>
            ))}
          </fieldset>
          {view === "monteCarlo" && (
            <label className="flex items-center gap-2">
              <span className="text-muted-foreground">
                paths
                <InfoIcon title={`Number of independent random-walk simulations. More paths = tighter percentile bands but slower. 1000 is the sweet spot. At 10000 the page may take 5-10 seconds to update on each slider drag.`} />
              </span>
              <input
                type="range"
                min={100}
                max={5000}
                step={100}
                value={nPaths}
                onChange={(e) => setNPaths(Number(e.target.value))}
                className="w-32"
                aria-label="n paths"
              />
              <span className="font-mono w-12 text-right">{nPaths}</span>
            </label>
          )}
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground">
              retirement age
              <InfoIcon title="Age at which you stop contributing to pension funds. Lower retirement age → smaller annuity at 67 (less time to compound contributions)." />
            </span>
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
            <span className="text-muted-foreground">
              tax (cap gains)
              <InfoIcon title="Israeli capital gains rate applied to portfolio income (the returns you withdraw). Default 25%. Pension annuity is NOT tax-adjusted in this model." />
            </span>
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
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground">
              μ nominal
              <InfoIcon
                title={`Expected portfolio return per year (nominal, before subtracting inflation). Default 0.08 = S&P 500 long-term historical. Drop to 0.04-0.05 for a stress-test of a flat/sideways decade. Real return (what you actually earn after inflation) = μ - 0.025.`}
              />
            </span>
            <input
              type="range"
              min={0.02}
              max={0.15}
              step={0.005}
              value={muNominal}
              onChange={(e) => setMuNominal(Number(e.target.value))}
              className="w-32"
              aria-label="mu nominal annual"
            />
            <span className="font-mono w-12 text-right">{(muNominal * 100).toFixed(1)}%</span>
          </label>
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground">
              σ (volatility)
              <InfoIcon title={`Portfolio annual volatility (standard deviation).
Default 0.18 = diversified-equity historical.
Crank up to 0.40-0.50 to model single-stock concentration risk — a NVDA-heavy portfolio's effective sigma is closer to 0.30-0.40 than 0.18.
Widens the bear/bull band; matters most at long horizons where sqrt(t) compounding makes the gap material.`} />
            </span>
            <input
              type="range"
              min={0.05}
              max={0.60}
              step={0.01}
              value={sigmaAnnual}
              onChange={(e) => setSigmaAnnual(Number(e.target.value))}
              className="w-32"
              aria-label="sigma annual"
            />
            <span className="font-mono w-12 text-right">{(sigmaAnnual*100).toFixed(0)}%</span>
          </label>
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground">
              lifestyle drift
              <InfoIcon title={`Extra expense-growth ABOVE the inflation rate (per year).
Default 0% means your expenses grow exactly with CPI.
Set to 1.5% to model personal lifestyle inflation running hotter than CPI (kids' costs, healthcare, lifestyle creep).
Affects expenses only — pension annuity still indexes to CPI alone.
Effective expense growth = inflation_annual + lifestyle_drift.`} />
            </span>
            <input
              type="range"
              min={0}
              max={0.05}
              step={0.005}
              value={lifestyleDriftAnnual}
              onChange={(e) => setLifestyleDriftAnnual(Number(e.target.value))}
              className="w-32"
              aria-label="lifestyle drift"
            />
            <span className="font-mono w-12 text-right">+{(lifestyleDriftAnnual*100).toFixed(1)}%</span>
          </label>
          <label className="flex items-center gap-2">
            <span className="text-muted-foreground">
              portfolio override (USD)
              <InfoIcon
                title={`Replace the DB-computed portfolio value with a what-if amount. Useful for scenarios like 'what if I sold NVDA today at 65% net? My portfolio drops from $3.8M to $2.99M — does retirement still pencil out?'. Leave empty (or click Reset) to use your actual current portfolio value.`}
              />
            </span>
            <input
              type="number"
              min={0}
              step={50000}
              placeholder={data ? `actual: ${formatPortfolioForPlaceholder(data)}` : ""}
              value={portfolioOverrideUsd ?? ""}
              onChange={(e) => {
                const v = e.target.value.trim();
                setPortfolioOverrideUsd(v === "" ? null : Number(v));
              }}
              className="w-32 px-2 py-0.5 text-xs border border-border/60 rounded bg-background"
              aria-label="portfolio value override USD"
            />
            {portfolioOverrideUsd != null && (
              <button
                type="button"
                onClick={() => setPortfolioOverrideUsd(null)}
                className="text-xs text-primary hover:underline"
              >
                reset
              </button>
            )}
          </label>
          {view === "deterministic" && <fieldset className="flex items-center gap-2 border-0 p-0 m-0">
            <legend className="text-muted-foreground sr-only">Scenario</legend>
            <span className="text-muted-foreground">
              scenario
              <InfoIcon title="Picks which lognormal band (bear/typical/bull) drives the headline retire-ready age and the tooltip surplus/shortfall. The other lines remain visible for comparison." />
            </span>
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
          </fieldset>}
          {view === "deterministic" && <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showBand}
              onChange={(e) => setShowBand(e.target.checked)}
            />
            <span>
              ±1σ band
              <InfoIcon title="Translucent fill showing the lognormal ±1σ band around the typical scenario. Visual heuristic, not a true forecast quantile. Width grows with sqrt(time)." />
            </span>
          </label>}
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showAnnuity}
              onChange={(e) => setShowAnnuity(e.target.checked)}
            />
            <span>
              pension annuity @ {annuityAge}
              <InfoIcon title="Israeli kupat_pensia + executive_insurance balances locked into a monthly stipend at age 67 (via mekadem = 200). Lower divisor = higher stipend; 200 is conservative." />
            </span>
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showLumpMarker}
              onChange={(e) => setShowLumpMarker(e.target.checked)}
            />
            <span>
              lump marker @ {lumpAge}
              <InfoIcon title="At age 60, Israeli law allows withdrawing keren_hishtalmut + kupat_gemel as a lump sum. The lump adds to the portfolio in this model." />
            </span>
          </label>
          {view === "deterministic" && <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={showRetireReady}
              onChange={(e) => setShowRetireReady(e.target.checked)}
            />
            <span>
              retire-ready marker
              <InfoIcon title="Vertical orange line at the earliest age where (portfolio income + pension annuity) >= inflated expenses, under the SELECTED scenario." />
            </span>
          </label>}
        </div>
      </CardHeader>
      <CardContent>
        {view === "monteCarlo" ? (
          mcLoading && !mcData ? (
            <p className="text-sm text-muted-foreground">Running {nPaths} simulations…</p>
          ) : mcError ? (
            <p className="text-sm text-error">Error: {mcError}</p>
          ) : mcData ? (
            renderMonteCarlo(mcData)
          ) : null
        ) : (
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
        )}
      </CardContent>
    </Card>
  );
}
