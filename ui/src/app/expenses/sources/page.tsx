"use client";

import { useEffect, useState } from "react";

import { SourceMonthlyTimeline } from "@/components/expenses/source-monthly-timeline";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  expensesApi,
  type SourceDetailResponse,
  type SourceOut,
} from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";
import { cn } from "@/lib/utils";

const USER_ID = "ariel";

const STATUS_DOT = {
  green: "bg-emerald-500",
  yellow: "bg-amber-500",
  red: "bg-rose-500",
  unknown: "bg-muted-foreground/40",
} as const;

export default function SourcesPage() {
  const [sources, setSources] = useState<SourceOut[]>([]);
  const [details, setDetails] = useState<Record<number, SourceDetailResponse>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const list = await expensesApi.sources(USER_ID);
        if (cancelled) return;
        setSources(list.sources);
        const detailEntries = await Promise.all(
          list.sources.map((s) =>
            expensesApi.sourceDetail(s.id, USER_ID)
              .then((d) => [s.id, d] as const)
              .catch(() => [s.id, null] as const),
          ),
        );
        if (cancelled) return;
        const map: Record<number, SourceDetailResponse> = {};
        for (const [id, d] of detailEntries) {
          if (d) map[id] = d;
        }
        setDetails(map);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (loading && sources.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground text-sm">
          Loading sources…
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {sources.map((s) => {
        const d = details[s.id];
        return (
          <Card key={s.id} id={`source-${s.id}`} className="scroll-mt-24">
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <span>{s.display_name}</span>
                <span className="text-xs text-muted-foreground font-normal">
                  {s.issuer} {s.external_id}
                  {s.cardholder_name ? ` · ${s.cardholder_name}` : ""}
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              {d ? (
                <>
                  <div className="text-xs text-muted-foreground mb-1">
                    Monthly activity (derived from transaction dates) — click a bar to drill in
                  </div>
                  <SourceMonthlyTimeline data={d.months ?? []} sourceId={s.id} />
                  <div className="text-xs text-muted-foreground mt-4 mb-1">
                    Per-statement reconciliation
                  </div>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-xs text-muted-foreground border-b border-border">
                        <th className="text-left py-2 pr-2">Period</th>
                        <th className="text-right py-2 px-2">Parsed</th>
                        <th className="text-right py-2 px-2">Declared</th>
                        <th className="text-right py-2 px-2">Gap</th>
                        <th className="text-right py-2 px-2">Tx</th>
                        <th className="text-right py-2 px-2">Card-paid</th>
                        <th className="text-right py-2 pl-2">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.statements.map((st) => (
                        <tr key={st.id} className="border-b border-border/60">
                          <td className="py-2 pr-2 tabular-nums whitespace-nowrap text-xs">
                            {st.period_start} → {st.period_end}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">
                            {st.parsed_total_nis !== null ? formatNIS(st.parsed_total_nis) : "—"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">
                            {st.declared_total_nis !== null ? formatNIS(st.declared_total_nis) : "—"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">
                            {st.gap !== null ? `${st.gap >= 0 ? "+" : ""}${st.gap.toFixed(2)}` : "—"}
                          </td>
                          <td className="py-2 px-2 text-right tabular-nums">{st.transaction_count}</td>
                          <td className="py-2 px-2 text-right tabular-nums">{st.correlated_count}</td>
                          <td className="py-2 pl-2 text-right">
                            <span className="inline-flex items-center gap-1.5">
                              <span className={cn("h-2 w-2 rounded-full", STATUS_DOT[st.status])} />
                              <span className="text-xs capitalize">{st.status}</span>
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              ) : (
                <div className="text-sm text-muted-foreground py-6 text-center">
                  Could not load detail for {s.display_name}.
                </div>
              )}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
