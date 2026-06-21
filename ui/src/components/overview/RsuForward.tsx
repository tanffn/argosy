"use client";

/**
 * RsuForward — compact bar chart of the deterministic forward RSU vest
 * projection: net NIS expected per year. READ-ONLY display (not wired into
 * the FI crossing). Year labels and the NIS axis are normalized locally.
 */

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  type TooltipContentProps,
  XAxis,
  YAxis,
} from "recharts";

interface RsuYear {
  year: number;
  net_nis: number;
}

export interface RsuForwardData {
  years: RsuYear[];
}

const BAR_COLOR = "#6366f1"; // indigo

function fmtNis(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `₪${(n / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `₪${(n / 1_000).toFixed(0)}K`;
  return `₪${n.toFixed(0)}`;
}

export function RsuForward({ data }: { data: RsuForwardData }) {
  const years = Array.isArray(data.years) ? data.years : [];
  if (years.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        Forward vest projection not available yet.
      </p>
    );
  }

  const renderTooltip = (tp: TooltipContentProps) => {
    if (!tp.active || !tp.payload || tp.payload.length === 0) return null;
    const row = tp.payload[0]?.payload as RsuYear | undefined;
    if (!row) return null;
    return (
      <div className="rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-sm">
        <div className="font-mono text-[10px] text-muted-foreground">
          {row.year}
        </div>
        <div className="mt-1 font-mono">{fmtNis(row.net_nis)} net</div>
      </div>
    );
  };

  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={years} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" opacity={0.18} vertical={false} />
        <XAxis
          dataKey="year"
          fontSize={11}
          tickFormatter={(v: number) => `${Math.round(v)}`}
          allowDecimals={false}
        />
        <YAxis fontSize={10} width={56} tickFormatter={(v) => fmtNis(v)} />
        <Tooltip content={renderTooltip} cursor={{ fill: "rgba(99,102,241,0.08)" }} />
        <Bar
          dataKey="net_nis"
          fill={BAR_COLOR}
          radius={[3, 3, 0, 0]}
          isAnimationActive={false}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}
