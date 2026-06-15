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
import { AgentTree } from "@/components/decisions/agent-tree";
import { VerdictCard } from "@/components/verdict-card";
import {
  api,
  type AgentTreeResponse,
  type CostBreakdown,
  type CostPhaseKey,
  type ReplayResponse,
} from "@/lib/api";

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

// Human-readable phase label rendered in the cost breakdown card. Keeps
// the layout compact ("Phase 1 analysts" rather than "phase_1") so the
// user can map a $-spend to the part of the synthesis pipeline it came
// from without staring at JSON keys.
const PHASE_LABELS: Record<CostPhaseKey, string> = {
  phase_1: "Phase 1 analysts",
  phase_2: "Phase 2 debates",
  phase_3: "Phase 3 synth",
  phase_4: "Phase 4 risk",
  phase_4_5_codex: "Phase 4.5 codex",
  phase_5: "Phase 5 FM",
};

function fmtUsd(n: number): string {
  // 4-decimal precision so per-agent rows under $0.01 don't show as $0.00,
  // matching the precision used in the per-phase participants table below.
  return `$${n.toFixed(2)}`;
}

function CostBreakdownCard({
  decisionRunId,
  breakdown,
}: {
  decisionRunId: number;
  breakdown: CostBreakdown;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Cost breakdown — Synthesis #{decisionRunId}
        </CardTitle>
        <CardDescription>
          Total: <span className="font-mono">{fmtUsd(breakdown.total_usd)}</span>{" "}
          · {breakdown.agent_count} agents called
        </CardDescription>
      </CardHeader>
      <CardContent className="grid grid-cols-1 md:grid-cols-2 gap-6 text-xs">
        <div>
          <p className="text-xs uppercase text-muted-foreground mb-2">
            By phase
          </p>
          <table className="w-full font-mono">
            <tbody>
              {breakdown.cost_per_phase_table.map((row) => (
                <tr
                  key={row.phase}
                  className="border-b border-border/40 last:border-b-0"
                >
                  <td className="py-1">{PHASE_LABELS[row.phase]}</td>
                  <td className="py-1 text-right">{fmtUsd(row.cost)}</td>
                  <td className="py-1 text-right text-muted-foreground pl-2">
                    {row.agent_count}×
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <p className="text-xs uppercase text-muted-foreground mb-2">
            By top role
          </p>
          {breakdown.top_3_agents.length === 0 ? (
            <p className="text-xs italic text-muted-foreground">
              No agent costs recorded.
            </p>
          ) : (
            <table className="w-full font-mono">
              <tbody>
                {breakdown.top_3_agents.map(([role, cost]) => (
                  <tr
                    key={role}
                    className="border-b border-border/40 last:border-b-0"
                  >
                    <td className="py-1">{role}</td>
                    <td className="py-1 text-right">{fmtUsd(cost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </CardContent>
    </Card>
  );
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

/**
 * The route id is usually a bare decision_run_id ("99"), but deep-links from the
 * cascade / audit surfaces use the audit token ("plan-synth-99"). Accept both by
 * taking the trailing integer; returns NaN when there is no parseable id (so the
 * page shows a clear error instead of firing /api/decisions/NaN/replay → 422).
 */
function parseDecisionRunId(raw: string): number {
  const direct = Number(raw);
  if (Number.isInteger(direct)) return direct;
  const m = raw.match(/(\d+)\s*$/);
  return m ? Number(m[1]) : NaN;
}

export default function DecisionReplayPage(props: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(props.params);
  const decisionRunId = parseDecisionRunId(id);
  const [data, setData] = useState<ReplayResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [transcripts, setTranscripts] = useState<Record<number, string>>({});
  // T0.6 — FM-rooted agent tree (separate fetch so it can fail independently
  // of the replay payload; older runs predating T0.4 may not have one).
  const [agentTree, setAgentTree] = useState<AgentTreeResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!Number.isFinite(decisionRunId)) {
      setError(`Invalid decision id "${id}" — expected a run id or a token like "plan-synth-99".`);
      setLoading(false);
      return;
    }
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
  }, [decisionRunId, id]);

  useEffect(() => {
    let cancelled = false;
    if (!Number.isFinite(decisionRunId)) return;
    (async () => {
      try {
        const r = await api.getAgentTree(decisionRunId, USER_ID);
        if (!cancelled) setAgentTree(r);
      } catch {
        // Tree fetch failures are non-fatal — the per-phase timeline
        // below still works. Legacy runs without decision_phases rows
        // will surface as a 404 here and we just skip the card.
        if (!cancelled) setAgentTree(null);
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

  // T0.6 — `sequence_mmd_full` from the replay payload is intentionally
  // unused now: the top-level mermaid diagram (which only showed phase
  // boundaries) has been replaced by the FM-rooted <AgentTree>. The
  // per-phase mermaid diagrams below still come from `p.sequence_mmd`.
  const { decision_run: run, phases, inputs } = data;

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

      {/* Cost breakdown — per-run observability. Surfaces the total
          spend + by-phase + top-role split so the user can see "this
          synthesis cost $X — $Y was the synthesizer, $Z was codex"
          without diving into agent_reports. Rendered ABOVE the agent
          tree because it answers the most common question ("how
          expensive was this run?") at a glance. Hidden when the run
          had no agent_reports at all (agent_count === 0). */}
      {agentTree && agentTree.cost_breakdown.agent_count > 0 && (
        <CostBreakdownCard
          decisionRunId={agentTree.decision_run_id}
          breakdown={agentTree.cost_breakdown}
        />
      )}

      {/* T0.6 — FM-rooted agent tree. Replaces the old top-level
          "Sequence (full run)" mermaid diagram, which only showed phase
          boundaries (not the actual who-fed-whom DAG). The per-phase
          mermaid diagrams further down still render via `p.sequence_mmd`.

          T4.4 — for non-synthesis kinds (delta_pushback, daily_brief,
          trade_proposal, plan_amendment_chat) the backend returns
          `root: null` + `unsupported_reason`. We render a small
          placeholder card so the user understands why the DAG view is
          missing for these kinds. */}
      {agentTree && agentTree.root && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Agent tree — Fund Manager at root
            </CardTitle>
            <CardDescription>
              {agentTree.status_summary.agents_ok} agents OK ·{" "}
              {agentTree.status_summary.agents_failed} failed
              {(agentTree.status_summary.agents_skipped ?? 0) > 0
                ? ` · ${agentTree.status_summary.agents_skipped} skipped`
                : ""}{" "}
              · {agentTree.status_summary.adapters_ok} adapters OK ·{" "}
              {agentTree.status_summary.adapters_failed} adapter failures
            </CardDescription>
          </CardHeader>
          <CardContent>
            <AgentTree root={agentTree.root} />
          </CardContent>
        </Card>
      )}
      {agentTree && !agentTree.root && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Agent tree — not available
            </CardTitle>
            <CardDescription>
              {agentTree.status_summary.agents_ok +
                agentTree.status_summary.agents_failed +
                (agentTree.status_summary.agents_skipped ?? 0)}{" "}
              agent run(s) recorded for this{" "}
              <span className="font-mono">{agentTree.decision_kind}</span>{" "}
              decision; the FM-rooted DAG is only built for synthesis runs.
            </CardDescription>
          </CardHeader>
          {agentTree.unsupported_reason && (
            <CardContent>
              <p className="text-xs text-muted-foreground italic">
                {agentTree.unsupported_reason}
              </p>
            </CardContent>
          )}
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
