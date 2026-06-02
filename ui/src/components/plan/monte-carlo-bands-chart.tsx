"use client";

/**
 * Wave 8 Piece D — Monte Carlo bands chart.
 *
 * Renders the P10/P50/P90 portfolio-value fan over time produced by
 * /api/plan/current/cashflow-monte-carlo. The card surfaces a
 * traffic-light verdict (green/amber/red) keyed off the
 * P(broke-before-95) probability so the user gets the bottom line
 * even without inspecting the chart.
 */

import { useMemo } from "react";
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
import type { MonteCarloProjectionResponse } from "@/lib/api";

interface MonteCarloBandsChartProps {
  response: MonteCarloProjectionResponse | null;
}

interface ChartRow {
  age_years: number;
  date: string;
  p10_p90_band: [number, number];
  p25_p75_band: [number, number];
  p50: number;
  p10: number;
  p90: number;
  fraction_solvent_pct: number;
}

function fmtUsd(v: unknown): string {
  if (Array.isArray(v)) return v.map((x) => fmtUsd(x)).join(" – ");
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `$${(n / 1_000).toFixed(0)}k`;
  return `$${n.toFixed(0)}`;
}

function fmtPct(p: number): string {
  return `${(p * 100).toFixed(1)}%`;
}

// Traffic-light tier keyed off P(broke before 95).
function verdictTier(pFailure95: number): "green" | "amber" | "red" {
  if (pFailure95 < 0.05) return "green";
  if (pFailure95 < 0.2) return "amber";
  return "red";
}

function tierBadgeClasses(tier: "green" | "amber" | "red"): string {
  switch (tier) {
    case "green":
      return "border-emerald-500/50 bg-emerald-500/10 text-emerald-500";
    case "amber":
      return "border-amber-500/50 bg-amber-500/10 text-amber-500";
    case "red":
      return "border-rose-500/50 bg-rose-500/10 text-rose-500";
  }
}

function tierHeadline(tier: "green" | "amber" | "red"): string {
  // Wave 8 v2 polish: explicit + plain-language phrasing — no
  // "off track" / "high risk" panic words that the user reads
  // before context. The full sentence beside this tag carries the
  // actual numbers.
  switch (tier) {
    case "green":
      return "Likely solvent";
    case "amber":
      return "Material variability";
    case "red":
      return "Stress-test fails";
  }
}

function findP50AtAge(
  series: MonteCarloProjectionResponse["series"],
  targetAge: number,
): number | null {
  // Pick the row whose age is closest to targetAge.
  let best: { diff: number; v: number } | null = null;
  for (const p of series) {
    const diff = Math.abs(p.age_years - targetAge);
    if (best == null || diff < best.diff) {
      best = { diff, v: p.portfolio_value_p50_usd };
    }
  }
  return best?.v ?? null;
}

function readNumberKey(
  obj: Record<string, unknown> | undefined,
  key: string,
  fallback: number,
): number {
  const v = obj?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

export function MonteCarloBandsChart({ response }: MonteCarloBandsChartProps) {
  const rows = useMemo<ChartRow[]>(() => {
    if (!response) return [];
    return response.series.map((p) => ({
      age_years: p.age_years,
      date: p.date,
      p10_p90_band: [p.portfolio_value_p10_usd, p.portfolio_value_p90_usd],
      p25_p75_band: [p.portfolio_value_p25_usd, p.portfolio_value_p75_usd],
      p50: p.portfolio_value_p50_usd,
      p10: p.portfolio_value_p10_usd,
      p90: p.portfolio_value_p90_usd,
      fraction_solvent_pct: p.fraction_solvent * 100,
    }));
  }, [response]);

  const lumpAge = useMemo(
    () => readNumberKey(response?.assumptions, "lump_pension_age", 60),
    [response],
  );
  const annuityAge = useMemo(
    () => readNumberKey(response?.assumptions, "annuity_age", 67),
    [response],
  );

  const xTicks = useMemo(() => {
    if (rows.length === 0) return [];
    const minAge = Math.floor(rows[0].age_years);
    const maxAge = Math.ceil(rows[rows.length - 1].age_years);
    const out: number[] = [];
    for (let a = minAge; a <= maxAge; a += 5) out.push(a);
    if (!out.includes(lumpAge)) out.push(lumpAge);
    if (!out.includes(annuityAge)) out.push(annuityAge);
    return out.sort((a, b) => a - b);
  }, [rows, lumpAge, annuityAge]);

  if (response == null) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Monte Carlo bands</CardTitle>
          <CardDescription>
            Monte Carlo projection unavailable. Run synthesis or check
            assumptions.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (response.series.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Monte Carlo bands</CardTitle>
          <CardDescription>No projection data.</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const tier = verdictTier(response.p_failure_before_age_95);

  const renderTooltip = (tp: TooltipContentProps) => {
    if (!tp.active || !tp.payload || tp.payload.length === 0) return null;
    const row = tp.payload[0]?.payload as ChartRow | undefined;
    if (!row) return null;
    return (
      <div className="rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-sm">
        <div className="font-mono text-[10px] text-muted-foreground">
          age {row.age_years.toFixed(1)} · {row.date}
        </div>
        <div className="mt-1 grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5">
          <span className="text-muted-foreground">P90 (best 10%)</span>
          <span className="font-mono">{fmtUsd(row.p90)}</span>
          <span className="text-muted-foreground">P75</span>
          <span className="font-mono">{fmtUsd(row.p25_p75_band[1])}</span>
          <span className="font-medium">P50 (median)</span>
          <span className="font-mono font-medium">{fmtUsd(row.p50)}</span>
          <span className="text-muted-foreground">P25</span>
          <span className="font-mono">{fmtUsd(row.p25_p75_band[0])}</span>
          <span className="text-muted-foreground">P10 (worst 10%)</span>
          <span className="font-mono">{fmtUsd(row.p10)}</span>
          <span className="border-t border-border/40 pt-1 text-muted-foreground">
            % paths solvent
          </span>
          <span className="border-t border-border/40 pt-1 font-mono">
            {row.fraction_solvent_pct.toFixed(1)}%
          </span>
        </div>
      </div>
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Monte Carlo bands · {response.n_paths.toLocaleString()} paths
        </CardTitle>
        <CardDescription>
          Portfolio-value percentile fan over time. P10/P50/P90 portfolio
          in USD; outer band is the P10–P90 spread, inner band P25–P75.
          Retire age assumed{" "}
          <span className="font-mono">{response.retirement_age_assumed}</span>.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ResponsiveContainer width="100%" height={360}>
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
            <YAxis
              fontSize={10}
              tickFormatter={(v) => fmtUsd(v)}
              width={72}
            />
            <Tooltip content={renderTooltip} />
            <Area
              type="monotone"
              dataKey="p10_p90_band"
              stroke="none"
              fill="#6366f1"
              fillOpacity={0.1}
              isAnimationActive={false}
              name="P10–P90 band"
            />
            <Area
              type="monotone"
              dataKey="p25_p75_band"
              stroke="none"
              fill="#6366f1"
              fillOpacity={0.2}
              isAnimationActive={false}
              name="P25–P75 band"
            />
            <Line
              type="monotone"
              dataKey="p50"
              stroke="#6366f1"
              strokeWidth={2.5}
              dot={false}
              isAnimationActive={false}
              name="median (P50)"
            />
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
          </ComposedChart>
        </ResponsiveContainer>

        <div
          className={`rounded-md border px-3 py-2 text-sm ${tierBadgeClasses(
            tier,
          )}`}
        >
          <p className="font-semibold uppercase tracking-wide text-xs mb-1">
            {tierHeadline(tier)}
          </p>
          {/* Narrative risk line (codex v2 polish): describe the bad
              tail in plain language before showing the probabilities,
              so a green-light plan doesn't read as alarming. */}
          <p className="text-sm text-foreground">
            {(() => {
              const median80 = findP50AtAge(response.series, 80);
              const median80Str =
                median80 != null && Number.isFinite(median80)
                  ? fmtUsd(median80)
                  : "—";
              const pFail95Pct = response.p_failure_before_age_95 * 100;
              // Use P(broke) as the truth source — P10 depletion-age is
              // noisy on a single-tick basis; the bulk failure stat is the
              // accurate "out of every N paths how many run out" reading.
              if (pFail95Pct < 0.5) {
                return (
                  <>
                    Across {response.n_paths.toLocaleString()} simulated
                    paths, virtually every path stays solvent through
                    age 95. Median path projects{" "}
                    <span className="font-mono">{median80Str}</span> at age 80.
                  </>
                );
              }
              if (pFail95Pct < 5) {
                return (
                  <>
                    Across {response.n_paths.toLocaleString()} simulated
                    paths, about{" "}
                    <span className="font-mono">{pFail95Pct.toFixed(1)}%</span>
                    {" "}deplete before age 95 — these are the bad-luck
                    sequence-of-returns paths. Median path projects{" "}
                    <span className="font-mono">{median80Str}</span> at age 80.
                  </>
                );
              }
              return (
                <>
                  Across {response.n_paths.toLocaleString()} simulated
                  paths,{" "}
                  <span className="font-mono">
                    {pFail95Pct.toFixed(1)}%
                  </span>{" "}
                  deplete before age 95 — that&apos;s meaningful tail
                  risk worth stress-testing against. Median path
                  projects <span className="font-mono">{median80Str}</span>{" "}
                  at age 80.
                </>
              );
            })()}
          </p>
          <p className="text-[11px] text-muted-foreground mt-2">
            P(deplete before 75):{" "}
            <span className="font-mono">
              {fmtPct(response.p_failure_before_age_75)}
            </span>{" "}
            · P(before 85):{" "}
            <span className="font-mono">
              {fmtPct(response.p_failure_before_age_85)}
            </span>{" "}
            · P(before 95):{" "}
            <span className="font-mono">
              {fmtPct(response.p_failure_before_age_95)}
            </span>
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
