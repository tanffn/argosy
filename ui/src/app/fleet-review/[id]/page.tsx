"use client";

/**
 * Full-report viewer for one Fleet self-review run.
 *
 * The detectors fire automatically from the orchestrator (post each
 * synthesis) and from the daily-brief loop (daily sweep).  This page
 * is the user-facing read surface: markdown body + the structured
 * findings table (a sister representation of the same data — markdown
 * for reading top-to-bottom, structured for filtering by severity).
 *
 * The home-page badge surfaces severity counts and links here.  Each
 * finding is rendered with severity + category + evidence JSON so the
 * user can see WHAT tripped the detector without re-running it.
 */

import { use, useEffect, useState } from "react";
import Link from "next/link";

import { Markdown } from "@/components/markdown";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import { api, type FleetSelfReviewDTO, type FleetSelfReviewFinding } from "@/lib/api";

const USER_ID = "ariel";

function toneFor(
  severity: "RED" | "AMBER" | "YELLOW",
): "error" | "warning" | "neutral" {
  if (severity === "RED") return "error";
  if (severity === "AMBER") return "warning";
  return "neutral";
}

export default function FleetReviewPage(props: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(props.params);
  const reportId = Number(id);
  const [data, setData] = useState<FleetSelfReviewDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setLoading(true);
        const r = await api.fleetSelfReview(USER_ID, reportId);
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
  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <div>
        <Link
          href="/fleet-review"
          className="text-xs font-mono text-info hover:underline"
        >
          &larr; all reports &amp; trends
        </Link>
      </div>
      <section className="rounded-lg border border-border bg-card/80 px-5 py-4 flex flex-col gap-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h1 className="font-mono text-xl font-semibold">
              Fleet self-review #{data.id}
            </h1>
            <p className="text-xs font-mono text-muted-foreground mt-1">
              {data.scope_kind} · generated{" "}
              {new Date(data.generated_at).toLocaleString()}
              {data.decision_run_id !== null
                ? ` · decision_run #${data.decision_run_id}`
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
        {data.decision_run_id !== null ? (
          <div>
            <Link
              href={`/decisions/${data.decision_run_id}`}
              className="text-xs font-mono text-info hover:underline"
            >
              -&gt; View the synthesis run that triggered this review
            </Link>
          </div>
        ) : null}
      </section>

      {data.findings.length > 0 ? (
        <section>
          <Card>
            <CardHeader>
              <CardTitle className="font-mono">
                Findings ({data.findings.length})
              </CardTitle>
              <CardDescription>
                Each row is one detector hit; click to expand the
                evidence block.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="flex flex-col gap-3">
                {data.findings.map((f) => (
                  <FindingRow key={f.id} finding={f} />
                ))}
              </ul>
            </CardContent>
          </Card>
        </section>
      ) : null}

      <section>
        <Card>
          <CardHeader>
            <CardTitle className="font-mono">Full report</CardTitle>
            <CardDescription>
              Deterministic markdown — no LLM step.  Detector findings
              are byte-identical to the structured list above.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="prose prose-sm max-w-none dark:prose-invert">
              <Markdown>{data.content_md}</Markdown>
            </div>
          </CardContent>
        </Card>
      </section>
    </main>
  );
}

function FindingRow({ finding }: { finding: FleetSelfReviewFinding }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded-md border border-border bg-card/60 p-3 flex flex-col gap-2">
      <button
        type="button"
        className="flex items-center justify-between gap-3 text-left"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="flex items-center gap-2 min-w-0">
          <StatusPill tone={toneFor(finding.severity)} mono>
            {finding.severity}
          </StatusPill>
          <span className="font-mono text-xs text-muted-foreground">
            {finding.detector}
          </span>
          <span className="font-mono text-sm truncate">
            {finding.title}
          </span>
        </span>
        <span className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
          {finding.category} · {open ? "hide" : "show"}
        </span>
      </button>
      {open ? (
        <div className="flex flex-col gap-2 mt-1">
          <pre className="text-[11px] font-mono whitespace-pre-wrap rounded bg-secondary/40 p-2 max-h-72 overflow-auto">
            {JSON.stringify(finding.evidence, null, 2)}
          </pre>
          {finding.suggested_fix ? (
            <p className="text-xs font-mono text-muted-foreground">
              <span className="font-semibold text-foreground">
                Suggested fix:
              </span>{" "}
              {finding.suggested_fix}
            </p>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
