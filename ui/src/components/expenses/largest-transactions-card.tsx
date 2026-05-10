"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { TransactionOut } from "@/lib/expenses/api";

interface Props {
  transactions: TransactionOut[];
  month: string | null;
}

function fmt(amt: number | null) {
  if (amt === null) return "—";
  return `₪${Math.round(Math.abs(amt)).toLocaleString("en-IL")}`;
}

export function LargestTransactionsCard({ transactions, month }: Props) {
  if (!transactions || transactions.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Largest transactions</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No spending transactions in this month.
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Largest transactions{" "}
          {month && (
            <span className="text-muted-foreground text-sm font-normal">— {month}</span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col">
        {transactions.map((t) => (
          <Link
            key={t.id}
            href={`/expenses/transactions?search=${encodeURIComponent(t.merchant_raw)}`}
            className="flex items-center justify-between text-sm py-1.5 px-2 -mx-2 hover:bg-secondary/40 rounded-sm"
          >
            <span className="truncate max-w-[60%]">{t.merchant_raw}</span>
            <span className="flex items-center gap-3">
              <span className="text-xs text-muted-foreground">{t.occurred_on}</span>
              <span className="tabular-nums">{fmt(t.amount_nis)}</span>
            </span>
          </Link>
        ))}
      </CardContent>
    </Card>
  );
}
