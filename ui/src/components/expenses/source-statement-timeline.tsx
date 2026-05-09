"use client";

import {
  Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { type StatementSummary } from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";

export function SourceStatementTimeline({ data }: { data: StatementSummary[] }) {
  const rows = data.map((s) => ({
    period: s.period_start.slice(0, 7),
    parsed: s.parsed_total_nis ?? 0,
  }));
  return (
    <ResponsiveContainer width="100%" height={140}>
      <BarChart data={rows} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
        <XAxis dataKey="period" fontSize={10} />
        <YAxis fontSize={10} tickFormatter={(v: number) => formatNIS(v)} width={70} />
        <Tooltip formatter={(v) => formatNIS(Number(v))} />
        <Bar dataKey="parsed" fill="hsl(220, 70%, 55%)" isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}
