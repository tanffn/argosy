"use client";

import { useMemo } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
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
import type { ProjectionResponse } from "@/lib/api";

interface ProjectionChartProps {
  data: ProjectionResponse | null;
}

interface Row {
  months_out: number;
  date: string;
  base: number;
  bull: number;
  bear: number;
  // Recharts needs the area band as a (lower, upper) tuple to render the
  // fill correctly. We synthesize that here.
  band: [number, number];
}

function fmtUsd(v: unknown): string {
  // Recharts can hand us strings, arrays (for two-valued series like the
  // band Area), or NaN depending on the chart element. Coerce defensively
  // so a stray non-number doesn't crash the tooltip.
  if (Array.isArray(v)) {
    return v.map((x) => fmtUsd(x)).join(" – ");
  }
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

function fmtTickDate(s: string): string {
  // s is "YYYY-MM"; show "'YY" annually-spaced.
  const [y, m] = s.split("-").map(Number);
  if (m === 1 || m === 6 || m === 12) return `'${String(y).slice(-2)}-${String(m).padStart(2, "0")}`;
  return "";
}

export function ProjectionChart(props: ProjectionChartProps) {
  const { data } = props;

  const rows = useMemo<Row[]>(() => {
    if (!data) return [];
    return data.series.map((p) => ({
      months_out: p.months_out,
      date: p.date,
      base: p.base,
      bull: p.bull,
      bear: p.bear,
      band: [p.bear, p.bull],
    }));
  }, [data]);

  if (!data || rows.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Portfolio value projection</CardTitle>
          <CardDescription>No projection available.</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const horizonYears = Math.round((rows.length - 1) / 12);
  const last = rows[rows.length - 1];

  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardTitle className="text-base">
          Portfolio value projection · {horizonYears}y
        </CardTitle>
        <CardDescription>
          Today: {fmtUsd(data.today_value_usd)} ·{" "}
          base in {horizonYears}y: {fmtUsd(last.base)} ·{" "}
          range: {fmtUsd(last.bear)} – {fmtUsd(last.bull)} ·{" "}
          safe monthly redraw (4% rule): {fmtUsd(data.safe_withdrawal_monthly_usd)}
          <br />
          <span className="text-[10px] font-mono opacity-70">
            Simplified parametric model (not Monte Carlo). mu={data.assumptions.mu_annual},
            σ={data.assumptions.sigma_annual}. Bands are ±1σ in log-return space.
          </span>
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={320}>
          <ComposedChart
            data={rows}
            margin={{ top: 8, right: 16, bottom: 4, left: 0 }}
          >
            <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
            <XAxis
              dataKey="date"
              fontSize={10}
              tickFormatter={fmtTickDate}
              minTickGap={20}
            />
            <YAxis
              fontSize={10}
              tickFormatter={fmtUsd}
              width={64}
            />
            <Tooltip
              formatter={((value: unknown, name: string) => {
                // The band Area series ships a tuple [bear, bull]; render
                // as a range. Other series pass scalars.
                if (name === "±1σ band" && Array.isArray(value)) {
                  return [fmtUsd(value), "bear–bull range"];
                }
                return [fmtUsd(value), name];
              }) as unknown as never}
            />
            {/* The bull/bear band rendered as a translucent Area between
                the two series. Painted first so the base Line sits on top. */}
            <Area
              type="monotone"
              dataKey="band"
              stroke="none"
              fill="#6366f1"
              fillOpacity={0.18}
              isAnimationActive={false}
              name="±1σ band"
            />
            <Line
              type="monotone"
              dataKey="base"
              stroke="#6366f1"
              strokeWidth={2.5}
              dot={false}
              isAnimationActive={false}
              name="base"
            />
            <Line
              type="monotone"
              dataKey="bull"
              stroke="#10b981"
              strokeDasharray="4 4"
              strokeWidth={1}
              dot={false}
              isAnimationActive={false}
              name="bull (+1σ)"
            />
            <Line
              type="monotone"
              dataKey="bear"
              stroke="#f43f5e"
              strokeDasharray="4 4"
              strokeWidth={1}
              dot={false}
              isAnimationActive={false}
              name="bear (-1σ)"
            />
          </ComposedChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
