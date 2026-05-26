"use client";

import { use, useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { ChevronDown, ChevronRight, FileText, Users } from "lucide-react";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { VerdictCard } from "@/components/verdict-card";
import { api, type ReplayResponse } from "@/lib/api";

// Mermaid touches `document` directly; lazy import disables SSR.
const MermaidDiagram = dynamic(
  () => import("@/components/mermaid-diagram").then((m) => m.MermaidDiagram),
  { ssr: false },
);

const USER_ID = "ariel";

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  const ms = parseAsUTC(iso);
  if (isNaN(ms)) return iso;
  return new Date(ms).toISOString().slice(0, 19).replace("T", " ") + "Z";
}

// Backend serializes decision_runs.started_at via plain .isoformat(),
// which drops the tzinfo for naive datetimes stored in SQLite. The JS
// Date constructor then interprets a tz-less ISO string as LOCAL time.
// Without this coercion the displayed duration is off by the local UTC
// offset (e.g. 38min synthesis appears as 218min for an Israel UTC+3
// reader). We treat any naive ISO string as UTC.
function parseAsUTC(iso: string): number {
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  return Date.parse(hasTz ? iso : iso + "Z");
}

function formatDuration(start: string, end: string | null): string {
  if (!end) return "running";
  const a = parseAsUTC(start);
  const b = parseAsUTC(end);
  const ms = b - a;
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${(s - m * 60).toFixed(0)}s`;
}

export default function DecisionReplayPage(props: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(props.params);
  const decisionRunId = Number(id);
  const [data, setData] = useState<ReplayResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [transcripts, setTranscripts] = useState<Record<number, string>>({});

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setLoading(true);
        const r = await api.getDecisionReplay(decisionRunId, USER_ID);
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
  }, [decisionRunId]);

  const toggleExpanded = async (phaseId: number) => {
    const next = new Set(expanded);
    if (next.has(phaseId)) {
      next.delete(phaseId);
    } else {
      next.add(phaseId);
      // Lazy-load the transcript on first expand.
      if (!transcripts[phaseId]) {
        try {
          const url = api.getPhaseTranscriptUrl(decisionRunId, phaseId, USER_ID);
          const res = await fetch(url, { cache: "no-store" });
          if (res.ok) {
            const txt = await res.text();
            setTranscripts((t) => ({ ...t, [phaseId]: txt }));
          } else {
            setTranscripts((t) => ({ ...t, [phaseId]: `(no transcript: ${res.status})` }));
          }
        } catch (e) {
          setTranscripts((t) => ({ ...t, [phaseId]: `(transcript fetch failed: ${e})` }));
        }
      }
    }
    setExpanded(next);
  };

  if (loading) {
    return (
      <main className="max-w-6xl mx-auto p-6">
        <p className="text-sm text-muted-foreground">Loading replay...</p>
      </main>
    );
  }
  if (error || !data) {
    return (
      <main className="max-w-6xl mx-auto p-6">
        <p className="text-sm text-error font-mono">
          {error ?? "decision not found"}
        </p>
      </main>
    );
  }

  const { decision_run: run, phases, inputs, sequence_mmd_full } = data;

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          Decision replay #{run.id}
        </h1>
        <p className="text-sm text-muted-foreground">
          {run.decision_kind ?? "decision"} · {run.ticker ?? "—"} · tier{" "}
          {run.tier ?? "—"} · status{" "}
          <span className="font-mono">{run.status ?? "—"}</span>
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Run metadata</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs font-mono">
          <div>
            <p className="text-muted-foreground">Started</p>
            <p>{formatTimestamp(run.started_at)}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Finished</p>
            <p>{formatTimestamp(run.finished_at)}</p>
          </div>
          <div>
            <p className="text-muted-foreground">Duration</p>
            <p>{formatDuration(run.started_at, run.finished_at)}</p>
          </div>
          <div>
            <p className="text-muted-foreground">FM</p>
            <p>{run.fund_manager_decision ?? "—"}</p>
          </div>
          {run.proposal_id !== null && (
            <div className="col-span-2 md:col-span-4">
              <a
                className="text-primary hover:underline"
                href="/proposals"
              >
                ↗ Proposal #{run.proposal_id}
              </a>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Inputs */}
      {inputs.user_files.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Inputs</CardTitle>
            <CardDescription>
              Files associated with this decision run.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="text-xs font-mono">
              {inputs.user_files.map((f) => (
                <li key={f.id} className="flex items-center gap-2 py-0.5">
                  <FileText className="h-3 w-3" aria-hidden suppressHydrationWarning />
                  <a
                    href={api.fileContentUrl(f.id, USER_ID)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-primary hover:underline"
                  >
                    {f.original_name}
                  </a>
                  <span className="text-muted-foreground">
                    {f.kind} · {f.source}
                  </span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* Sequence diagram (full) */}
      {sequence_mmd_full && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Sequence (full run)</CardTitle>
          </CardHeader>
          <CardContent>
            <MermaidDiagram src={sequence_mmd_full} className="overflow-auto" />
          </CardContent>
        </Card>
      )}

      {/* Per-phase timeline */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Negotiation timeline</CardTitle>
          <CardDescription>
            Each phase represents one structured verdict produced by the
            multi-agent flow. Click a row to see the transcript.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {phases.length === 0 && (
            <p className="text-xs text-muted-foreground italic">
              No phases recorded. Either this is a legacy decision (pre-Wave-C)
              or the recorder failed during the run — see audit log for
              `provenance.phase.failed`.
            </p>
          )}
          {phases.map((p) => {
            const open = expanded.has(p.id);
            const ChevIcon = open ? ChevronDown : ChevronRight;
            return (
              <div
                key={p.id}
                className="border border-border rounded-md bg-secondary/20"
              >
                <button
                  type="button"
                  onClick={() => toggleExpanded(p.id)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-secondary/40"
                >
                  <ChevIcon className="h-4 w-4 text-muted-foreground" aria-hidden suppressHydrationWarning />
                  <span className="font-mono text-xs text-muted-foreground">
                    #{p.seq}
                  </span>
                  <span className="font-semibold text-sm">{p.kind}</span>
                  <span className="text-xs text-muted-foreground">
                    {formatDuration(p.started_at, p.finished_at)}
                  </span>
                  <span className="text-xs text-muted-foreground inline-flex items-center gap-1 ml-auto">
                    <Users className="h-3 w-3" aria-hidden suppressHydrationWarning />
                    {p.participants.length}
                  </span>
                  {p.verdict_kind && (
                    <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-primary/20 text-primary">
                      {p.verdict_kind}
                    </span>
                  )}
                </button>

                {open && (
                  <div className="px-4 py-3 flex flex-col gap-4 border-t border-border">
                    <div>
                      <p className="text-xs uppercase text-muted-foreground mb-1">
                        Verdict
                      </p>
                      <VerdictCard
                        verdictKind={p.verdict_kind}
                        verdict={p.verdict}
                      />
                    </div>

                    {p.tldr_md && (
                      <details>
                        <summary className="cursor-pointer text-xs uppercase text-muted-foreground">
                          TL;DR markdown
                        </summary>
                        <pre className="text-xs font-mono whitespace-pre-wrap mt-2 bg-background border border-border rounded-md p-3">
                          {p.tldr_md}
                        </pre>
                      </details>
                    )}

                    {p.participants.length > 0 && (
                      <div>
                        <p className="text-xs uppercase text-muted-foreground mb-1">
                          Participants
                        </p>
                        <table className="text-xs font-mono w-full">
                          <thead>
                            <tr className="text-muted-foreground border-b border-border">
                              <th className="text-left py-1">role</th>
                              <th className="text-left py-1">side / perspective</th>
                              <th className="text-left py-1">round</th>
                              <th className="text-left py-1">confidence</th>
                              <th className="text-left py-1">model</th>
                              <th className="text-right py-1">tokens</th>
                              <th className="text-right py-1">cost</th>
                            </tr>
                          </thead>
                          <tbody>
                            {p.participants.map((pp) => (
                              <tr key={pp.agent_report_id} className="border-b border-border/40">
                                <td className="py-1">{pp.agent_role}</td>
                                <td className="py-1">
                                  {pp.side ?? pp.perspective ?? "—"}
                                </td>
                                <td className="py-1">{pp.round ?? "—"}</td>
                                <td className="py-1">{pp.confidence ?? "—"}</td>
                                <td className="py-1">{pp.model ?? "—"}</td>
                                <td className="py-1 text-right">
                                  {pp.tokens_in !== null && pp.tokens_out !== null
                                    ? `${pp.tokens_in}+${pp.tokens_out}`
                                    : "—"}
                                </td>
                                <td className="py-1 text-right">
                                  {pp.cost_usd !== null
                                    ? `$${pp.cost_usd.toFixed(4)}`
                                    : "—"}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}

                    {p.sequence_mmd && (
                      <details>
                        <summary className="cursor-pointer text-xs uppercase text-muted-foreground">
                          Sequence (this phase)
                        </summary>
                        <div className="mt-2 overflow-auto">
                          <MermaidDiagram src={p.sequence_mmd} />
                        </div>
                      </details>
                    )}

                    <details>
                      <summary className="cursor-pointer text-xs uppercase text-muted-foreground">
                        Full transcript
                      </summary>
                      <pre className="text-xs font-mono whitespace-pre-wrap mt-2 bg-background border border-border rounded-md p-3 max-h-[24rem] overflow-auto">
                        {transcripts[p.id] ?? "(loading...)"}
                      </pre>
                    </details>
                  </div>
                )}
              </div>
            );
          })}
        </CardContent>
      </Card>
    </main>
  );
}
