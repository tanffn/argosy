"use client";

import { useRouter } from "next/navigation";
import {
  Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { type MonthBucket } from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";

interface SourceMonthlyTimelineProps {
  data: MonthBucket[];
  sourceId: number;
}

function endOfMonth(yyyymm: string): string {
  const [y, m] = yyyymm.split("-").map(Number);
  // Day 0 of next month = last day of current month.
  const d = new Date(y, m, 0);
  return `${y}-${String(m).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/**
 * Per-month bar chart of debit_nis for a source. Click a bar to drill into
 * the transactions filtered by source + month range. This is derived from
 * tx.occurred_on so it's consistent across issuers — Discount Bank ships
 * one big file but rendered as 16 monthly bars instead of 2 statement bars.
 */
export function SourceMonthlyTimeline({
  data, sourceId,
}: SourceMonthlyTimelineProps) {
  const router = useRouter();
  const rows = data.map((m) => ({
    month: m.month,
    debit: m.debit_nis,
    credit: m.credit_nis,
    n: m.transaction_count,
  }));

  function onBarClick(payload: unknown) {
    if (!payload || typeof payload !== "object") return;
    const p = payload as { month?: string };
    if (!p.month) return;
    const fromDate = `${p.month}-01`;
    const toDate = endOfMonth(p.month);
    router.push(
      `/expenses/transactions?source_id=${sourceId}&from_date=${fromDate}&to_date=${toDate}`,
    );
  }

  return (
    <ResponsiveContainer width="100%" height={140}>
      <BarChart data={rows} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
        <XAxis dataKey="month" fontSize={10} />
        <YAxis fontSize={10} tickFormatter={(v: number) => formatNIS(v)} width={70} />
        <Tooltip formatter={(v) => formatNIS(Number(v))} />
        <Bar
          dataKey="debit"
          fill="hsl(220, 70%, 55%)"
          isAnimationActive={false}
          onClick={onBarClick}
          style={{ cursor: "pointer" }}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}
