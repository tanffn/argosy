"use client";

/**
 * Cashflow coverage (inflow vs outflow) chart.
 *
 * The Monte Carlo bands chart plots portfolio VALUE, where the P50
 * dominates the y-scale and the pension's effect on the household's
 * cashflow is invisible. This chart instead plots, over age, how the
 * monthly spend is COVERED by the income streams the plan draws on:
 * the portfolio net draw (bottom of the stack), the age-67 pension
 * annuity, and the Bituach Leumi (national insurance) stipend. The
 * expenses line is the spend those inflows must cover.
 *
 * The age-60 pension lump is a one-time event, so it is shown as a
 * distinct labeled marker rather than folded into the monthly stack
 * (where its single-tick spike would dwarf everything else).
 *
 * Data source: /api/plan/current/cashflow-monte-carlo (the same
 * response the MonteCarloBandsChart consumes), using the per-tick
 * deterministic income-composition fields.
 */

import { useMemo } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceDot,
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

interface CashflowInflowOutflowChartProps {
  response: MonteCarloProjectionResponse | null;
}

interface ChartRow {
  age_years: number;
  date: string;
  portfolio_net_draw: number;
  pension_annuity: number;
  bl: number;
  expenses: number;
  lump: number;
}

function fmtUsd(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `$${(n / 1_000).toFixed(1)}k`;
  return `$${n.toFixed(0)}`;
}

function readNumberKey(
  obj: Record<string, unknown> | undefined,
  key: string,
  fallback: number,
): number {
  const v = obj?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : fallback;
}

// Muted palette, aligned with the bands chart (indigo family + a
// teal/emerald for the pension streams that "appear" at age 67).
const COLOR_PORTFOLIO = "#6366f1"; // indigo — living off the portfolio
const COLOR_ANNUITY = "#10b981"; // emerald — pension annuity
const COLOR_BL = "#14b8a6"; // teal — Bituach Leumi
const COLOR_EXPENSES = "#f43f5e"; // rose — the spend line
const COLOR_LUMP = "#a3a3a3"; // muted grey — one-time lump marker

export function CashflowInflowOutflowChart({
  response,
}: CashflowInflowOutflowChartProps) {
  const rows = useMemo<ChartRow[]>(() => {
    if (!response) return [];
    return response.series.map((p) => ({
      age_years: p.age_years,
      date: p.date,
      portfolio_net_draw: p.portfolio_net_draw_monthly_usd,
      pension_annuity: p.pension_annuity_monthly_usd,
      bl: p.bl_monthly_usd,
      expenses: p.expenses_monthly_usd,
      lump: p.lump_amount_usd,
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

  // The one-time lump shows as a labeled marker, not a stacked band.
  // Pick the single tick where the lump is largest (the unlock tick).
  const lumpMarker = useMemo<{ age: number; amount: number } | null>(() => {
    let best: { age: number; amount: number } | null = null;
    for (const r of rows) {
      if (r.lump > 0 && (best == null || r.lump > best.amount)) {
        best = { age: r.age_years, amount: r.lump };
      }
    }
    return best;
  }, [rows]);

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
          <CardTitle className="text-base">Cashflow coverage</CardTitle>
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
          <CardTitle className="text-base">Cashflow coverage</CardTitle>
          <CardDescription>No projection data.</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const renderTooltip = (tp: TooltipContentProps) => {
    if (!tp.active || !tp.payload || tp.payload.length === 0) return null;
    const row = tp.payload[0]?.payload as ChartRow | undefined;
    if (!row) return null;
    const inflow = row.portfolio_net_draw + row.pension_annuity + row.bl;
    return (
      <div className="rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-sm">
        <div className="font-mono text-[10px] text-muted-foreground">
          age {row.age_years.toFixed(1)} · {row.date}
        </div>
        <div className="mt-1 grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5">
          <span className="text-muted-foreground">Portfolio net draw</span>
          <span className="font-mono">{fmtUsd(row.portfolio_net_draw)}/mo</span>
          <span className="text-muted-foreground">Pension annuity</span>
          <span className="font-mono">{fmtUsd(row.pension_annuity)}/mo</span>
          <span className="text-muted-foreground">Bituach Leumi</span>
          <span className="font-mono">{fmtUsd(row.bl)}/mo</span>
          <span className="border-t border-border/40 pt-1 font-medium">
            Total inflow
          </span>
          <span className="border-t border-border/40 pt-1 font-mono font-medium">
            {fmtUsd(inflow)}/mo
          </span>
          <span className="text-muted-foreground">Expenses (spend)</span>
          <span className="font-mono">{fmtUsd(row.expenses)}/mo</span>
          {row.lump > 0 ? (
            <>
              <span className="border-t border-border/40 pt-1 text-muted-foreground">
                One-time lump
              </span>
              <span className="border-t border-border/40 pt-1 font-mono">
                {fmtUsd(row.lump)}
              </span>
            </>
          ) : null}
        </div>
      </div>
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Cashflow coverage</CardTitle>
        <CardDescription>
          How the monthly spend is covered, over age. The stack is the
          inflow that funds the household: before{" "}
          <span className="font-mono">{annuityAge}</span> it&apos;s almost
          all portfolio net draw (you&apos;re living off the portfolio); at{" "}
          <span className="font-mono">{annuityAge}</span> the pension
          annuity and Bituach Leumi bands appear and the portfolio band
          shrinks — that&apos;s the visible bridge. The rose line is the
          spend the inflows must cover; the age-
          <span className="font-mono">{lumpAge}</span> marker is the
          one-time lump unlock (shown as a point, not folded into the
          monthly stack).
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
              dataKey="portfolio_net_draw"
              stackId="inflow"
              stroke={COLOR_PORTFOLIO}
              strokeWidth={0.5}
              fill={COLOR_PORTFOLIO}
              fillOpacity={0.35}
              isAnimationActive={false}
              name="portfolio net draw"
            />
            <Area
              type="monotone"
              dataKey="pension_annuity"
              stackId="inflow"
              stroke={COLOR_ANNUITY}
              strokeWidth={0.5}
              fill={COLOR_ANNUITY}
              fillOpacity={0.4}
              isAnimationActive={false}
              name="pension annuity"
            />
            <Area
              type="monotone"
              dataKey="bl"
              stackId="inflow"
              stroke={COLOR_BL}
              strokeWidth={0.5}
              fill={COLOR_BL}
              fillOpacity={0.4}
              isAnimationActive={false}
              name="Bituach Leumi"
            />
            <Line
              type="monotone"
              dataKey="expenses"
              stroke={COLOR_EXPENSES}
              strokeWidth={2.5}
              dot={false}
              isAnimationActive={false}
              name="expenses (spend)"
            />
            <ReferenceLine
              x={lumpAge}
              stroke={COLOR_LUMP}
              strokeDasharray="3 3"
              label={{
                value: `lump @ ${lumpAge}`,
                position: "top",
                fill: COLOR_LUMP,
                fontSize: 10,
              }}
            />
            <ReferenceLine
              x={annuityAge}
              stroke={COLOR_ANNUITY}
              strokeDasharray="3 3"
              label={{
                value: `annuity @ ${annuityAge}`,
                position: "top",
                fill: COLOR_ANNUITY,
                fontSize: 10,
              }}
            />
            {lumpMarker ? (
              <ReferenceDot
                x={lumpMarker.age}
                y={0}
                r={5}
                fill={COLOR_LUMP}
                stroke="#525252"
                label={{
                  value: `lump ${fmtUsd(lumpMarker.amount)}`,
                  position: "insideBottomLeft",
                  fill: COLOR_LUMP,
                  fontSize: 10,
                }}
              />
            ) : null}
          </ComposedChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
