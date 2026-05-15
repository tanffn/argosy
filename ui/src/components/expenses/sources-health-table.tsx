"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type SourceHealthEntry } from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";
import { cn } from "@/lib/utils";

const STATUS_DOT = {
  green: "bg-success",
  yellow: "bg-warning",
  red: "bg-error",
  unknown: "bg-muted-foreground/40",
} as const;

export function SourcesHealthTable({ data }: { data: SourceHealthEntry[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Sources & reconciliation</CardTitle>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <div className="text-sm text-muted-foreground py-6 text-center">
            No sources registered.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-muted-foreground border-b border-border">
                <th className="text-left py-2 pr-2">Source</th>
                <th className="text-left py-2 px-2">Latest period</th>
                <th className="text-right py-2 px-2">Parsed</th>
                <th className="text-right py-2 px-2">Declared</th>
                <th className="text-right py-2 px-2">Gap</th>
                <th className="text-right py-2 px-2">Stmts</th>
                <th className="text-right py-2 pl-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {data.map((s) => (
                <tr key={s.source_id} className="border-b border-border/60 hover:bg-secondary/40">
                  <td className="py-2 pr-2">
                    <Link
                      href={`/expenses/sources?source_id=${s.source_id}`}
                      className="hover:underline"
                    >
                      {s.display_name}
                    </Link>
                    <span className="ml-2 text-xs text-muted-foreground">
                      {s.issuer} {s.external_id}
                    </span>
                  </td>
                  <td className="py-2 px-2 text-muted-foreground tabular-nums">
                    {s.last_period ?? "—"}
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums">
                    {s.parsed_total_nis !== null ? formatNIS(s.parsed_total_nis) : "—"}
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums">
                    {s.declared_total_nis !== null ? formatNIS(s.declared_total_nis) : "—"}
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums">
                    {s.gap !== null
                      ? <span className={s.status === "red" ? "text-error" : ""}>
                          {s.gap >= 0 ? "+" : ""}{s.gap.toFixed(2)}
                        </span>
                      : "—"}
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums">{s.statement_count}</td>
                  <td className="py-2 pl-2 text-right">
                    <span className="inline-flex items-center gap-1.5">
                      <span className={cn("h-2 w-2 rounded-full", STATUS_DOT[s.status])} />
                      <span className="text-xs capitalize">{s.status}</span>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}
