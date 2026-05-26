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
// Each step subtracts an even fraction of `reduction.remaining` so the
// cumulative line bends down quarterly.
const REDUCTION_PROGRAM_QUARTERS = 8;

function parseMonthMs(date: string): number {
  // Accept "YYYY-MM" or "YYYY-MM-DD". Default day = 15 for month-only so
  // events land in the middle of the month rather than at the boundary.
  const s = date.length === 7 ? `${date}-15` : date;
  return Date.parse(`${s}T00:00:00Z`);
}

function buildSeries(data: NvdaTrajectoryResponse): SeriesPoint[] {
  if (data.today_shares == null) return [];
  const todayMs = Date.parse(data.today_date + "T00:00:00Z");
  if (Number.isNaN(todayMs)) return [];

  // ----- Build the FUTURE half (vests + reduction program) -----
  type Event = { t_ms: number; delta: number };
  const futureEvents: Event[] = [];
  for (const v of data.vests) {
    const ms = parseMonthMs(v.date);
    if (!Number.isNaN(ms) && ms >= todayMs) {
      futureEvents.push({ t_ms: ms, delta: v.shares });
    }
  }
  const remaining = data.reduction_program.remaining;
  if (remaining && remaining > 0) {
    const perStep = Math.round(remaining / REDUCTION_PROGRAM_QUARTERS);
    for (let i = 1; i <= REDUCTION_PROGRAM_QUARTERS; i++) {
      const ms = todayMs + i * 90 * 86400_000;
      futureEvents.push({ t_ms: ms, delta: -perStep });
    }
  }
  futureEvents.sort((a, b) => a.t_ms - b.t_ms);

  // ----- Build the PAST half from sales history -----
  // We can't perfectly reconstruct past share counts because we don't have
  // past vest events recorded — only the future schedule. So we approximate
  // by walking BACKWARDS from today's share count: pre-sale = today + sales
  // that occurred AFTER that month. This shows the user the curve their
  // sales drew on the share count, ignoring past vest noise.
  const pastEvents: Event[] = data.past_sales
    .map((s) => ({ t_ms: parseMonthMs(s.date), delta: s.shares }))
    .filter((e) => !Number.isNaN(e.t_ms) && e.t_ms < todayMs)
    .sort((a, b) => a.t_ms - b.t_ms);

  const points: SeriesPoint[] = [];

  // Walk past events forward from earliest, with starting share count =
  // today_shares + sum(all past sales) (i.e. before any of them happened).
  const totalPastSold = pastEvents.reduce((s, e) => s + e.delta, 0);
  let running = data.today_shares + totalPastSold;
  for (const e of pastEvents) {
    // Plot a point just BEFORE the sale (current running count) and just
    // AFTER (running - shares_sold), so the chart shows the step-down at
    // the sale month.
    points.push({
      t_ms: e.t_ms - 86400_000, // 1 day before
      date_iso: new Date(e.t_ms - 86400_000).toISOString().slice(0, 10),
      shares: running,
    });
    running -= e.delta;
    points.push({
      t_ms: e.t_ms,
      date_iso: new Date(e.t_ms).toISOString().slice(0, 10),
      shares: running,
    });
  }

  // Today's anchor point.
  points.push({
    t_ms: todayMs,
    date_iso: data.today_date,
    shares: data.today_shares,
  });
  running = data.today_shares;
  for (const e of futureEvents) {
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
      const ms = parseMonthMs(v.date);
      if (Number.isNaN(ms)) continue;
      running += v.shares;
      dots.push({ t_ms: ms, shares: running, note: v.note });
    }
    return dots;
  }, [data]);

  // Sale dots show where the user actually transacted, plotted at the
  // share-count immediately AFTER each sale.
  const saleDots = useMemo(() => {
    if (!data || data.today_shares == null) return [];
    const totalSold = data.past_sales.reduce((s, x) => s + x.shares, 0);
    let running = data.today_shares + totalSold;
    const dots: Array<{ t_ms: number; shares: number; note: string }> = [];
    for (const s of data.past_sales) {
      const ms = parseMonthMs(s.date);
      if (Number.isNaN(ms)) continue;
      running -= s.shares;
      dots.push({
        t_ms: ms,
        shares: running,
        note: `Sold ${s.shares.toLocaleString()}${
          s.price_usd != null ? ` @ $${s.price_usd}` : ""
        }`,
      });
    }
    return dots;
  }, [data]);

  const todayMs = useMemo(
    () => (data ? Date.parse(data.today_date + "T00:00:00Z") : null),
    [data],
  );

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
              {todayMs != null && (
                <ReferenceLine
                  x={todayMs}
                  stroke="#94a3b8"
                  strokeDasharray="2 4"
                  label={{
                    value: "today",
                    position: "top",
                    fill: "#94a3b8",
                    fontSize: 10,
                  }}
                />
              )}
              {vestDots.map((v, i) => (
                <ReferenceDot
                  key={`vest-${i}`}
                  x={v.t_ms}
                  y={v.shares}
                  r={5}
                  fill="#22d3ee"
                  stroke="#0e7490"
                />
              ))}
              {saleDots.map((s, i) => (
                <ReferenceDot
                  key={`sale-${i}`}
                  x={s.t_ms}
                  y={s.shares}
                  r={5}
                  fill="#f43f5e"
                  stroke="#9f1239"
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
        {data && (data.vests.length > 0 || data.past_sales.length > 0) && (
          <div className="mt-3 text-xs grid grid-cols-1 lg:grid-cols-2 gap-x-6 gap-y-1">
            {data.past_sales.length > 0 && (
              <ul>
                <li className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1">
                  Past sales ({data.past_sales.length})
                </li>
                {data.past_sales.map((s, i) => (
                  <li key={i} className="flex items-baseline gap-2">
                    <span className="inline-block w-2 h-2 rounded-full bg-rose-500 flex-shrink-0" />
                    <span className="font-mono text-muted-foreground">{s.date}</span>
                    <span className="font-mono">−{s.shares}</span>
                    {s.price_usd != null && (
                      <span className="text-muted-foreground text-[10px]">
                        @ ${s.price_usd}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {data.vests.length > 0 && (
              <ul>
                <li className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1">
                  Upcoming vests ({data.vests.length})
                </li>
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
          </div>
        )}
      </CardContent>
    </Card>
  );
}
