"use client";

// Sprint A commit #8 — last-20-runs panel rendered inside an expanded
// JobRow. Skipped runs are collapsed-by-default behind a disclosure
// (IMPORTANT #4 — SKIPPED rows are surfaced but not in the operator's
// face). output_summary is a collapsed <details> per row.

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { StatusPill } from "@/components/ui/status-pill";
import { api, type JobRunRow, type JobRunStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

const RUNS_LIMIT = 20;

function statusTone(s: JobRunStatus | null | undefined) {
  switch (s) {
    case "ok":
    case "connected":
      return "success" as const;
    case "running":
    case "reconnecting":
    case "starting":
      return "accent" as const;
    case "error":
    case "stopped":
    case "cancelled":
      return "error" as const;
    case "skipped":
      return "neutral" as const;
    default:
      return "neutral" as const;
  }
}

function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s - m * 60);
  return `${m}m ${rem}s`;
}

function formatStartedAt(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function JobRunHistory({ jobName }: { jobName: string }) {
  const [runs, setRuns] = useState<JobRunRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSkipped, setShowSkipped] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.jobs.listRuns(jobName, { limit: RUNS_LIMIT });
        if (!cancelled) {
          setRuns(data.runs);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [jobName]);

  if (error) {
    return (
      <div className="text-xs text-error px-4 py-3">
        Failed to load run history: {error}
      </div>
    );
  }
  if (runs === null) {
    return (
      <div className="text-xs text-muted-foreground px-4 py-3">
        Loading run history…
      </div>
    );
  }
  if (runs.length === 0) {
    return (
      <div className="text-xs text-muted-foreground px-4 py-3">
        No runs recorded yet.
      </div>
    );
  }

  const skipped = runs.filter((r) => r.status === "skipped");
  const visible = showSkipped ? runs : runs.filter((r) => r.status !== "skipped");

  return (
    <div className="px-4 py-3 bg-background/40 border-t border-border/60">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-muted-foreground">
          Last {runs.length} runs
        </span>
        {skipped.length > 0 && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setShowSkipped((v) => !v)}
          >
            {showSkipped
              ? `Hide ${skipped.length} skipped`
              : `Show ${skipped.length} skipped`}
          </Button>
        )}
      </div>
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-muted-foreground border-b border-border/40">
            <th className="text-left py-1.5 pr-2">Started</th>
            <th className="text-left py-1.5 px-2">Status</th>
            <th className="text-right py-1.5 px-2">Duration</th>
            <th className="text-left py-1.5 px-2">Trigger</th>
            <th className="text-left py-1.5 pl-2">Output</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((r) => (
            <tr
              key={r.id}
              className={cn(
                "border-b border-border/30 align-top",
                r.status === "error" && "bg-error/5",
              )}
            >
              <td className="py-1.5 pr-2 tabular-nums text-muted-foreground whitespace-nowrap">
                {formatStartedAt(r.started_at)}
              </td>
              <td className="py-1.5 px-2">
                <StatusPill tone={statusTone(r.status)} mono>
                  {r.status}
                </StatusPill>
              </td>
              <td className="py-1.5 px-2 text-right tabular-nums text-muted-foreground">
                {formatDuration(r.duration_ms)}
              </td>
              <td className="py-1.5 px-2 text-muted-foreground">
                {r.triggered_by ?? "—"}
              </td>
              <td className="py-1.5 pl-2">
                {r.error_message ? (
                  <span className="text-error">{r.error_message}</span>
                ) : r.output_summary &&
                  Object.keys(r.output_summary).length > 0 ? (
                  <details className="cursor-pointer">
                    <summary className="text-muted-foreground select-none">
                      summary
                    </summary>
                    <pre className="text-[10px] font-mono whitespace-pre-wrap text-muted-foreground mt-1">
                      {JSON.stringify(r.output_summary, null, 2)}
                    </pre>
                  </details>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
