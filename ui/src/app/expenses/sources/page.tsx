"use client";

import { useEffect, useState } from "react";

import { SourceMonthlyTimeline } from "@/components/expenses/source-monthly-timeline";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  expensesApi,
  type SourceDetailResponse,
  type SourceOut,
  type StatementSummary,
} from "@/lib/expenses/api";
import { formatNIS } from "@/lib/expenses/format";
import { cn } from "@/lib/utils";

const USER_ID = "ariel";

const STATUS_DOT = {
  green: "bg-success",
  yellow: "bg-warning",
  red: "bg-error",
  unknown: "bg-muted-foreground/40",
} as const;

// Hole #4 — per-source last-sync indicator. Compute the most recent
// statement's period_end so a stale source is visible without scanning
// the per-statement table. Returns null when the source has no
// statements ingested yet.
function computeLastSync(
  statements: StatementSummary[],
): { dateISO: string; daysAgo: number } | null {
  if (statements.length === 0) return null;
  let maxIso = "";
  for (const st of statements) {
    if (!st.period_end) continue;
    if (st.period_end > maxIso) maxIso = st.period_end;
  }
  if (!maxIso) return null;
  const ms = Date.parse(maxIso);
  if (Number.isNaN(ms)) return null;
  const daysAgo = Math.floor((Date.now() - ms) / (1000 * 60 * 60 * 24));
  return { dateISO: maxIso, daysAgo };
}

// Tone bands picked for monthly-cadence sources (Leumi statements
// land within ~5 days of month-end). 30 days = on cadence; 31-60 =
// one month behind; >60 = a real gap worth flagging.
function lastSyncTone(daysAgo: number): "success" | "warning" | "error" {
  if (daysAgo <= 30) return "success";
  if (daysAgo <= 60) return "warning";
  return "error";
}

function lastSyncToneClass(tone: "success" | "warning" | "error"): string {
  if (tone === "success") return "text-emerald-400 border-emerald-400/30 bg-emerald-400/10";
  if (tone === "warning") return "text-amber-400 border-amber-400/30 bg-amber-400/10";
  return "text-rose-400 border-rose-400/30 bg-rose-400/10";
}

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
        const lastSync = d ? computeLastSync(d.statements) : null;
        return (
          <Card key={s.id} id={`source-${s.id}`} className="scroll-mt-24">
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2 flex-wrap">
                <span>{s.display_name}</span>
                <span className="text-xs text-muted-foreground font-normal">
                  {s.issuer} {s.external_id}
                  {s.cardholder_name ? ` · ${s.cardholder_name}` : ""}
                </span>
                {lastSync ? (
                  <span
                    className={cn(
                      "ml-auto text-[10px] font-mono font-medium px-2 py-0.5 rounded-full border tabular-nums",
                      lastSyncToneClass(lastSyncTone(lastSync.daysAgo)),
                    )}
                    title={`Most recent statement ends ${lastSync.dateISO}`}
                  >
                    last statement: {lastSync.dateISO} (
                    {lastSync.daysAgo === 0
                      ? "today"
                      : lastSync.daysAgo === 1
                        ? "1 day ago"
                        : `${lastSync.daysAgo} days ago`}
                    )
                  </span>
                ) : d ? (
                  <span className="ml-auto text-[10px] font-mono font-medium px-2 py-0.5 rounded-full border border-muted-foreground/30 bg-muted-foreground/10 text-muted-foreground">
                    no statements ingested
                  </span>
                ) : null}
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
