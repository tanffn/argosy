"use client";

import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ReferenceLine,
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
import type { NvdaTrajectoryResponse } from "@/lib/api";

interface NvdaTrajectoryChartProps {
  data: NvdaTrajectoryResponse | null;
}

interface SeriesPoint {
  t_ms: number;
  date_iso: string;
  shares: number;
}

// Distribute the reduction program across N quarterly steps starting today.
// Mirrors the spec section 2.2 logic: each step subtracts an even fraction
// of `reduction.remaining` so the cumulative line bends down quarterly.
const REDUCTION_PROGRAM_QUARTERS = 8;

function buildSeries(data: NvdaTrajectoryResponse): SeriesPoint[] {
  if (data.today_shares == null) return [];
  const todayMs = Date.parse(data.today_date + "T00:00:00Z");
  if (Number.isNaN(todayMs)) return [];

  // Build a list of (timestamp, sharesDelta, label) events.
  type Event = { t_ms: number; delta: number };
  const events: Event[] = [];

  for (const v of data.vests) {
    const ms = Date.parse(v.date + (v.date.length === 7 ? "-15" : "") + "T00:00:00Z");
    if (!Number.isNaN(ms)) events.push({ t_ms: ms, delta: v.shares });
  }

  const remaining = data.reduction_program.remaining;
  if (remaining && remaining > 0) {
    const perStep = Math.round(remaining / REDUCTION_PROGRAM_QUARTERS);
    for (let i = 1; i <= REDUCTION_PROGRAM_QUARTERS; i++) {
      const ms = todayMs + i * 90 * 86400_000;
      events.push({ t_ms: ms, delta: -perStep });
    }
  }

  events.sort((a, b) => a.t_ms - b.t_ms);

  const points: SeriesPoint[] = [{
    t_ms: todayMs,
    date_iso: data.today_date,
    shares: data.today_shares,
  }];
  let running = data.today_shares;
  for (const e of events) {
    running += e.delta;
    points.push({
      t_ms: e.t_ms,
      date_iso: new Date(e.t_ms).toISOString().slice(0, 10),
      shares: running,
    });
  }
  return points;
}

function fmtTick(ms: number): string {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function NvdaTrajectoryChart(props: NvdaTrajectoryChartProps) {
  const { data } = props;

  const series = useMemo(() => (data ? buildSeries(data) : []), [data]);

  const vestDots = useMemo(() => {
    if (!data || data.today_shares == null) return [];
    let running = data.today_shares;
    const dots: Array<{ t_ms: number; shares: number; note: string }> = [];
    for (const v of data.vests) {
      const ms = Date.parse(v.date + (v.date.length === 7 ? "-15" : "") + "T00:00:00Z");
      if (Number.isNaN(ms)) continue;
      running += v.shares;
      dots.push({ t_ms: ms, shares: running, note: v.note });
    }
    return dots;
  }, [data]);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">NVDA share trajectory</CardTitle>
        <CardDescription>
          {data?.today_shares != null
            ? `Today: ${data.today_shares.toLocaleString()} shares`
            : "today's share count unavailable"}
          {data?.ceiling_target_shares != null
            ? ` · long-horizon ceiling: ${data.ceiling_target_shares.toLocaleString()}`
            : ""}
          {data?.reduction_program.remaining
            ? ` · ${data.reduction_program.remaining.toLocaleString()} shares left in reduction program (${data.reduction_program.progress_pct}% done)`
            : ""}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!data || series.length === 0 ? (
          <p className="text-sm text-muted-foreground py-8 text-center">
            No NVDA data available.
          </p>
        ) : (
          <ResponsiveContainer width="100%" height={280}>
            <LineChart
              data={series}
              margin={{ top: 10, right: 16, bottom: 4, left: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
              <XAxis
                dataKey="t_ms"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={fmtTick}
                fontSize={11}
              />
              <YAxis
                dataKey="shares"
                fontSize={11}
                tickFormatter={(v) => v.toLocaleString()}
              />
              <Tooltip
                cursor={false}
                formatter={((value: number) => [
                  `${value.toLocaleString()} sh`,
                  "shares",
                ]) as unknown as never}
                labelFormatter={(_label, items) => {
                  const it = Array.isArray(items) && items[0] && typeof items[0] === "object"
                    ? (items[0] as { payload?: SeriesPoint }).payload
                    : undefined;
                  return it ? it.date_iso : "";
                }}
              />
              <Line
                type="monotone"
                dataKey="shares"
                stroke="#6366f1"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
              {data.ceiling_target_shares != null && (
                <ReferenceLine
                  y={data.ceiling_target_shares}
                  stroke="#f97316"
                  strokeDasharray="4 4"
                  label={{
                    value: `ceiling ${data.ceiling_target_shares.toLocaleString()}`,
                    position: "insideTopRight",
                    fill: "#f97316",
                    fontSize: 10,
                  }}
                />
              )}
              {vestDots.map((v, i) => (
                <ReferenceDot
                  key={i}
                  x={v.t_ms}
                  y={v.shares}
                  r={5}
                  fill="#22d3ee"
                  stroke="#0e7490"
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
        {data && data.vests.length > 0 && (
          <ul className="mt-3 text-xs grid grid-cols-2 gap-x-4 gap-y-1">
            {data.vests.map((v, i) => (
              <li key={i} className="flex items-baseline gap-2">
                <span className="inline-block w-2 h-2 rounded-full bg-cyan-400 flex-shrink-0" />
                <span className="font-mono text-muted-foreground">{v.date}</span>
                <span className="font-mono">+{v.shares}</span>
                <span className="text-muted-foreground text-[10px] truncate">
                  {v.note}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
