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

interface Marker {
  t_ms: number;
  shares: number;
  note: string;
}

interface Trajectory {
  line: SeriesPoint[];
  vestMarkers: Marker[];
  plannedSellMarkers: Marker[];
  pastSaleMarkers: Marker[];
}

const EMPTY_TRAJECTORY: Trajectory = {
  line: [],
  vestMarkers: [],
  plannedSellMarkers: [],
  pastSaleMarkers: [],
};

function parseMonthMs(date: string): number {
  // Accept "YYYY-MM" or "YYYY-MM-DD". Default day = 15 for month-only so
  // events land in the middle of the month rather than at the boundary.
  const s = date.length === 7 ? `${date}-15` : date;
  return Date.parse(`${s}T00:00:00Z`);
}

function iso(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

const QUARTER_MS = 91 * 86400_000;

/**
 * One running-share-count walk: past sales (down), today, then the future as a
 * REALISTIC zigzag — each RSU vest steps the count UP, each ~quarterly sell
 * steps it DOWN — landing exactly on the canonical target (the 13% cap). The
 * total sold is sized to absorb today's shares PLUS all future vests minus the
 * target, so vesting is taken into account. Vest + sell markers read their y
 * off this same line so they sit on it (not floating).
 */
function buildTrajectory(data: NvdaTrajectoryResponse): Trajectory {
  if (data.today_shares == null) return EMPTY_TRAJECTORY;
  const todayMs = Date.parse(data.today_date + "T00:00:00Z");
  if (Number.isNaN(todayMs)) return EMPTY_TRAJECTORY;
  const todayShares = data.today_shares;

  const line: SeriesPoint[] = [];
  const vestMarkers: Marker[] = [];
  const plannedSellMarkers: Marker[] = [];
  const pastSaleMarkers: Marker[] = [];

  // ----- PAST half: reconstruct by walking BACKWARDS from today -----
  const pastEvents = data.past_sales
    .map((s) => ({ t_ms: parseMonthMs(s.date), shares: s.shares, price: s.price_usd }))
    .filter((e) => !Number.isNaN(e.t_ms) && e.t_ms < todayMs)
    .sort((a, b) => a.t_ms - b.t_ms);
  const totalPastSold = pastEvents.reduce((s, e) => s + e.shares, 0);
  let running = todayShares + totalPastSold;
  for (const e of pastEvents) {
    line.push({ t_ms: e.t_ms - 86400_000, date_iso: iso(e.t_ms - 86400_000), shares: running });
    running -= e.shares;
    line.push({ t_ms: e.t_ms, date_iso: iso(e.t_ms), shares: running });
    pastSaleMarkers.push({
      t_ms: e.t_ms,
      shares: running,
      note: `Sold ${e.shares.toLocaleString()}${e.price != null ? ` @ $${e.price}` : ""}`,
    });
  }

  // Today's anchor.
  line.push({ t_ms: todayMs, date_iso: data.today_date, shares: todayShares });

  // ----- FUTURE half: vests UP + quarterly sells DOWN to the target -----
  const target = data.ceiling_target_shares;
  const path = data.projected_path ?? [];
  const targetMs = path.length ? Date.parse(path[path.length - 1].date + "T00:00:00Z") : null;
  const futureVests = data.vests
    .map((v) => ({ t_ms: parseMonthMs(v.date), shares: v.shares, note: v.note }))
    .filter((e) => !Number.isNaN(e.t_ms) && e.t_ms > todayMs);

  if (target != null && targetMs != null && targetMs > todayMs) {
    const totalVest = futureVests.reduce((s, e) => s + e.shares, 0);
    const totalToSell = todayShares + totalVest - target;
    const sellDates: number[] = [];
    for (let d = todayMs + QUARTER_MS; d <= targetMs + 1; d += QUARTER_MS) {
      sellDates.push(Math.min(d, targetMs));
    }
    if (sellDates.length === 0) sellDates.push(targetMs);
    const perSell = totalToSell > 0 ? totalToSell / sellDates.length : 0;

    type Ev = { t_ms: number; delta: number; kind: "vest" | "sell"; note: string };
    const events: Ev[] = [
      ...futureVests.map((v) => ({ t_ms: v.t_ms, delta: v.shares, kind: "vest" as const, note: v.note })),
      ...sellDates.map((ms) => ({ t_ms: ms, delta: -perSell, kind: "sell" as const, note: "" })),
    ].sort((a, b) => a.t_ms - b.t_ms);

    running = todayShares;
    let prevShares = todayShares;
    for (const e of events) {
      running += e.delta;
      const sh = Math.max(0, Math.round(running));
      line.push({ t_ms: e.t_ms, date_iso: iso(e.t_ms), shares: sh });
      if (e.kind === "vest") {
        vestMarkers.push({ t_ms: e.t_ms, shares: sh, note: `Vest +${e.delta.toLocaleString()} → ${sh.toLocaleString()} sh` });
      } else {
        plannedSellMarkers.push({
          t_ms: e.t_ms,
          shares: sh,
          note: `Planned sell ~${Math.round(prevShares - sh).toLocaleString()} → ${sh.toLocaleString()} sh`,
        });
      }
      prevShares = sh;
    }
    // Land exactly on the target.
    if (line.length && line[line.length - 1].shares !== target) {
      line.push({ t_ms: targetMs, date_iso: iso(targetMs), shares: target });
    }
  }

  return { line, vestMarkers, plannedSellMarkers, pastSaleMarkers };
}

function fmtTick(ms: number): string {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function NvdaTrajectoryChart(props: NvdaTrajectoryChartProps) {
  const { data } = props;

  const traj = useMemo(
    () => (data ? buildTrajectory(data) : EMPTY_TRAJECTORY),
    [data],
  );
  const series = traj.line;
  const vestDots = traj.vestMarkers;
  const saleDots = traj.pastSaleMarkers;
  const plannedSellDots = traj.plannedSellMarkers;

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
          {plannedSellDots.length > 0 ? " · ○ = planned sells" : ""}
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
              {plannedSellDots.map((s, i) => (
                <ReferenceDot
                  key={`planned-sell-${i}`}
                  x={s.t_ms}
                  y={s.shares}
                  r={4}
                  fill="transparent"
                  stroke="#f43f5e"
                  strokeWidth={2}
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
