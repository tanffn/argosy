"use client";

/**
 * Full viewer for one EX2 anomaly-detection report.
 *
 * The runner auto-fires from the expense ingest path (event-driven)
 * and from the daily-brief loop (daily backstop). The home banner
 * surfaces the first RED anomaly + links here for the full report:
 * structured anomaly list + per-watchlist-entry status snapshot.
 *
 * Cross-tenant access returns 404 from the backend; an unknown id
 * does the same — never reveals existence cross-tenant.
 */

import { use, useEffect, useState } from "react";
import Link from "next/link";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type AnomalyItem,
  type AnomalyReportDTO,
  type AnomalyWatchlistStatus,
} from "@/lib/api";

const USER_ID = "ariel";

function toneFor(
  severity: "RED" | "AMBER" | "YELLOW",
): "error" | "warning" | "neutral" {
  if (severity === "RED") return "error";
  if (severity === "AMBER") return "warning";
  return "neutral";
}

function stateTone(
  state: AnomalyWatchlistStatus["state"],
): "success" | "warning" | "error" | "neutral" {
  if (state === "ALERT") return "error";
  if (state === "RESOLVED") return "success";
  if (state === "UNKNOWN") return "neutral";
  return "neutral"; // NORMAL
}

export default function AnomalyReportPage(props: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(props.params);
  const reportId = Number(id);
  const [data, setData] = useState<AnomalyReportDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setLoading(true);
        const r = await api.anomalyById(USER_ID, reportId);
        if (!cancelled) setData(r);
      } catch (e: unknown) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reportId]);

  if (loading) {
    return (
      <main className="max-w-6xl mx-auto p-6">
        <p className="text-sm font-mono text-muted-foreground">Loading...</p>
      </main>
    );
  }
  if (error) {
    return (
      <main className="max-w-6xl mx-auto p-6">
        <p className="text-sm font-mono text-error">Error: {error}</p>
      </main>
    );
  }
  if (data === null) {
    return (
      <main className="max-w-6xl mx-auto p-6">
        <p className="text-sm font-mono text-muted-foreground">
          Report not found.
        </p>
      </main>
    );
  }

  const sev = data.severity_summary;
  const anomalies = data.report?.anomalies ?? [];
  const watchlist = data.report?.watchlist_status ?? [];
  const cited = data.report?.cited_sources ?? [];
  const runnerError = data.report?._runner_error;

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <section className="rounded-lg border border-border bg-card/80 px-5 py-4 flex flex-col gap-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h1 className="font-mono text-xl font-semibold">
              Anomaly report #{data.id}
            </h1>
            <p className="text-xs font-mono text-muted-foreground mt-1">
              triggered by {data.triggered_by} · {" "}
              {new Date(data.triggered_at).toLocaleString()}
              {data.source_statement_id !== null
                ? ` · statement #${data.source_statement_id}`
                : ""}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <StatusPill tone="error" mono>
              RED {sev.RED ?? 0}
            </StatusPill>
            <StatusPill tone="warning" mono>
              AMBER {sev.AMBER ?? 0}
            </StatusPill>
            <StatusPill tone="neutral" mono>
              YELLOW {sev.YELLOW ?? 0}
            </StatusPill>
          </div>
        </div>
        <div>
          <Link
            href="/"
            className="text-xs font-mono text-info hover:underline"
          >
            -&gt; Back to home
          </Link>
        </div>
      </section>

      {runnerError ? (
        <section>
          <Card>
            <CardHeader>
              <CardTitle className="font-mono text-error">
                Runner error
              </CardTitle>
              <CardDescription>
                The runner caught an exception from the agent. The row
                is preserved so the timeline is honest about every fire.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="text-[11px] font-mono whitespace-pre-wrap rounded bg-secondary/40 p-2 max-h-72 overflow-auto">
                {runnerError}
              </pre>
            </CardContent>
          </Card>
        </section>
      ) : null}

      {anomalies.length > 0 ? (
        <section>
          <Card>
            <CardHeader>
              <CardTitle className="font-mono">
                Detected anomalies ({anomalies.length})
              </CardTitle>
              <CardDescription>
                One row per anomaly the agent flagged this run. Sorted
                by severity (RED first).
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="flex flex-col gap-3">
                {[...anomalies]
                  .sort((a, b) =>
                    severityRank(a.severity) - severityRank(b.severity),
                  )
                  .map((a, idx) => (
                    <AnomalyRow key={idx} anomaly={a} />
                  ))}
              </ul>
            </CardContent>
          </Card>
        </section>
      ) : (
        <section>
          <Card>
            <CardHeader>
              <CardTitle className="font-mono">No anomalies</CardTitle>
              <CardDescription>
                The agent evaluated the watchlist for this run and found
                no deviations. Per-entry status below.
              </CardDescription>
            </CardHeader>
          </Card>
        </section>
      )}

      {watchlist.length > 0 ? (
        <section>
          <Card>
            <CardHeader>
              <CardTitle className="font-mono">
                Watchlist status ({watchlist.length})
              </CardTitle>
              <CardDescription>
                One row per watchlist entry the agent evaluated.
                NORMAL = expected pattern observed. ALERT = anomaly row
                exists above. RESOLVED = back to normal vs prior run.
                UNKNOWN = no statement covered this entry&apos;s account.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="flex flex-col gap-2">
                {watchlist.map((w, idx) => (
                  <li
                    key={idx}
                    className="rounded-md border border-border bg-card/60 p-3 flex items-center justify-between gap-3 flex-wrap"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <StatusPill tone={stateTone(w.state)} mono>
                        {w.state}
                      </StatusPill>
                      <span className="font-mono text-sm truncate">
                        {w.name}
                      </span>
                    </div>
                    <span className="font-mono text-[11px] text-muted-foreground truncate max-w-full">
                      {w.last_evidence || "—"}
                    </span>
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        </section>
      ) : null}

      {cited.length > 0 ? (
        <section>
          <Card>
            <CardHeader>
              <CardTitle className="font-mono">Cited sources</CardTitle>
              <CardDescription>
                Source IDs the agent referenced when justifying its
                anomalies above. Format: ``watchlist:&lt;name&gt;`` or
                ``statement:&lt;id&gt;``.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="flex flex-col gap-1">
                {cited.map((c, idx) => (
                  <li
                    key={idx}
                    className="font-mono text-xs text-muted-foreground"
                  >
                    {c}
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        </section>
      ) : null}
    </main>
  );
}

function severityRank(severity: "RED" | "AMBER" | "YELLOW"): number {
  if (severity === "RED") return 0;
  if (severity === "AMBER") return 1;
  return 2;
}

function AnomalyRow({ anomaly }: { anomaly: AnomalyItem }) {
  return (
    <li className="rounded-md border border-border bg-card/60 p-3 flex flex-col gap-2">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <span className="flex items-center gap-2 min-w-0">
          <StatusPill tone={toneFor(anomaly.severity)} mono>
            {anomaly.severity}
          </StatusPill>
          <span className="font-mono text-sm truncate">
            {anomaly.watchlist_entry_name}
          </span>
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">
          last observed: {anomaly.last_seen || "—"}
        </span>
      </div>
      <p className="text-sm font-mono text-foreground">
        {anomaly.observation}
      </p>
      <p className="text-xs font-mono text-muted-foreground">
        <span className="font-semibold text-foreground">Suggested:</span>{" "}
        {anomaly.suggested_action}
      </p>
    </li>
  );
}
