"use client";

// Sprint A commit #8 — main registry table.
//
// Polls /api/jobs every 5 s while the document is visible, 30 s when
// blurred (visibilitychange). Each row is expandable to reveal
// <JobRunHistory />. Health renders a small dot with the color from
// JobView.health (per spec §5 — derived server-side, UI never branches).

import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import { api, type JobView, type JobRunStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

import { JobRunHistory } from "./JobRunHistory";
import { RunNowButton } from "./RunNowButton";

const POLL_VISIBLE_MS = 5000;
const POLL_HIDDEN_MS = 30000;

const HEALTH_DOT: Record<JobView["health"], string> = {
  green: "bg-success",
  amber: "bg-warning",
  red: "bg-error",
  unknown: "bg-muted-foreground/40",
};

const HEALTH_TOOLTIP: Record<JobView["health"], string> = {
  green: "Healthy — last run ok within cadence window.",
  amber: "Stale or running long — check history.",
  red: "Failing or stopped — needs attention.",
  unknown: "No data yet, or health derivation unavailable.",
};

const KIND_VARIANT: Record<
  string,
  "success" | "warning" | "error" | "info" | "default" | "secondary"
> = {
  ingest: "info",
  monitor: "warning",
  maintenance: "secondary",
  notification: "success",
};

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

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diffMs = Date.now() - then;
  const absS = Math.abs(diffMs) / 1000;
  const dir = diffMs >= 0 ? "ago" : "from now";
  if (absS < 60) return `${Math.round(absS)} s ${dir}`;
  const absM = absS / 60;
  if (absM < 60) return `${Math.round(absM)} min ${dir}`;
  const absH = absM / 60;
  if (absH < 24) return `${Math.round(absH)} h ${dir}`;
  const absD = absH / 24;
  return `${Math.round(absD)} d ${dir}`;
}

function clip(text: string | null, max = 80): string {
  if (!text) return "—";
  if (text.length <= max) return text;
  return text.slice(0, max) + "…";
}

export function JobsTable() {
  const [jobs, setJobs] = useState<JobView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const refresh = useCallback(async () => {
    try {
      const data = await api.jobs.list();
      setJobs(data.jobs);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    refresh();
  }, [refresh]);

  // Visibility-aware polling per spec ("every 5s while focused, every
  // 30s when blurred").
  useEffect(() => {
    let handle: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (handle) clearInterval(handle);
      const ms =
        typeof document !== "undefined" && document.visibilityState === "hidden"
          ? POLL_HIDDEN_MS
          : POLL_VISIBLE_MS;
      handle = setInterval(refresh, ms);
    };
    start();
    const onVis = () => start();
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVis);
    }
    return () => {
      if (handle) clearInterval(handle);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVis);
      }
    };
  }, [refresh]);

  const toggle = (name: string) =>
    setExpanded((prev) => ({ ...prev, [name]: !prev[name] }));

  const sorted = useMemo(() => {
    if (!jobs) return null;
    // Sort: red first, amber next, then by name. Surfaces problems.
    const rank = { red: 0, amber: 1, unknown: 2, green: 3 } as const;
    return [...jobs].sort((a, b) => {
      const r = rank[a.health] - rank[b.health];
      if (r !== 0) return r;
      return a.metadata.name.localeCompare(b.metadata.name);
    });
  }, [jobs]);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Jobs registry</CardTitle>
        <CardDescription>
          Cadence loops + long-running jobs. Click a row to view recent
          history. Run-now requires the admin token (set above).
        </CardDescription>
      </CardHeader>
      <CardContent>
        {error && (
          <p className="text-sm text-error font-mono mb-3">{error}</p>
        )}
        {sorted === null && !error && (
          <p className="text-sm text-muted-foreground">Loading jobs…</p>
        )}
        {sorted && sorted.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No jobs registered. Check that the scheduler booted
            (<code>ARGOSY_RUN_SCHEDULER=1</code>).
          </p>
        )}
        {sorted && sorted.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-wide text-muted-foreground border-b border-border">
                <th className="text-left py-2 pr-2 w-[60px]">Health</th>
                <th className="text-left py-2 px-2">Name</th>
                <th className="text-left py-2 px-2 w-[100px]">Kind</th>
                <th className="text-left py-2 px-2 w-[200px]">Schedule</th>
                <th className="text-left py-2 px-2 w-[140px]">Last run</th>
                <th className="text-left py-2 px-2 w-[120px]">Status</th>
                <th className="text-left py-2 px-2">Error</th>
                <th className="text-left py-2 px-2 w-[140px]">Next</th>
                <th className="text-right py-2 pl-2 w-[180px]">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((job) => {
                const open = !!expanded[job.metadata.name];
                return (
                  <JobRowFragment
                    key={job.metadata.name}
                    job={job}
                    open={open}
                    onToggle={() => toggle(job.metadata.name)}
                    onChanged={refresh}
                  />
                );
              })}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  );
}

// Inline row — kept here (not split into a separate file) per the
// commit brief: "new (or inline in JobsTable — your call)". Co-located
// because the row layout is tightly coupled to the table's columns.
function JobRowFragment({
  job,
  open,
  onToggle,
  onChanged,
}: {
  job: JobView;
  open: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  return (
    <>
      <tr
        className={cn(
          "border-b border-border/60 hover:bg-secondary/40 cursor-pointer",
          open && "bg-secondary/30",
        )}
        onClick={onToggle}
      >
        <td className="py-2 pr-2">
          <span
            className="inline-flex items-center gap-1.5"
            title={HEALTH_TOOLTIP[job.health]}
          >
            <span
              className={cn("h-2.5 w-2.5 rounded-full", HEALTH_DOT[job.health])}
              aria-label={`health: ${job.health}`}
            />
          </span>
        </td>
        <td className="py-2 px-2">
          <div className="flex flex-col">
            <span className="font-medium">{job.metadata.name}</span>
            <span className="text-xs text-muted-foreground line-clamp-1">
              {job.metadata.description}
            </span>
          </div>
        </td>
        <td className="py-2 px-2">
          <Badge variant={KIND_VARIANT[job.metadata.source_kind] ?? "default"}>
            {job.metadata.source_kind}
          </Badge>
          {job.metadata.long_running && (
            <Badge variant="outline" className="ml-1">
              long-running
            </Badge>
          )}
        </td>
        <td className="py-2 px-2">
          <div className="flex flex-col">
            <span>{job.metadata.schedule_human}</span>
            {job.metadata.schedule_cron && (
              <code className="text-[10px] text-muted-foreground">
                {job.metadata.schedule_cron}
              </code>
            )}
          </div>
        </td>
        <td className="py-2 px-2 text-muted-foreground tabular-nums">
          {relativeTime(job.last_run_at)}
        </td>
        <td className="py-2 px-2">
          {job.last_run_status ? (
            <StatusPill tone={statusTone(job.last_run_status)} mono>
              {job.last_run_status}
            </StatusPill>
          ) : (
            <span className="text-muted-foreground text-xs">never</span>
          )}
        </td>
        <td className="py-2 px-2 text-xs">
          {job.last_run_error ? (
            <span
              className="text-error font-mono"
              title={job.last_run_error}
            >
              {clip(job.last_run_error, 60)}
            </span>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </td>
        <td className="py-2 px-2 text-muted-foreground tabular-nums">
          {job.metadata.long_running ? (
            <span className="text-xs italic">n/a</span>
          ) : (
            relativeTime(job.next_run_at)
          )}
        </td>
        <td
          className="py-2 pl-2 text-right"
          onClick={(e) => e.stopPropagation()}
        >
          <RunNowButton job={job} onChanged={onChanged} />
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={9} className="p-0">
            <JobRunHistory jobName={job.metadata.name} />
          </td>
        </tr>
      )}
    </>
  );
}
