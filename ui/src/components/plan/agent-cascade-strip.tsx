"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";

import { api, type AgentActivityRow } from "@/lib/api";

interface AgentCascadeStripProps {
  userId: string;
  decisionId: string | null;
  fmRejected: boolean;
  onNodeClick?: (agentRole: string) => void;
}

// Phase grouping for the 5-phase synthesis fleet. Order matches the
// orchestrator's pipeline. Names match `agent_reports.agent_role` exactly.
const PHASES: Array<{ label: string; roles: string[] }> = [
  {
    label: "Phase 1: Analysts",
    roles: [
      "fundamentals",
      "technical",
      "news",
      "sentiment",
      "macro",
      "fx",
      "tax",
      "concentration",
      "risk_officer",
    ],
  },
  {
    label: "Phase 2: Debate",
    roles: ["bull_researcher", "bear_researcher", "researcher_facilitator"],
  },
  {
    label: "Phase 3: Synth",
    roles: ["plan_synthesizer"],
  },
  {
    label: "Phase 4: Risk",
    roles: ["risk_facilitator"],
  },
  {
    label: "Phase 5: Fund Manager",
    roles: ["fund_manager"],
  },
];

function nodeColor(present: boolean, isFm: boolean, fmRejected: boolean) {
  if (!present) return "bg-muted-foreground/20 border-muted-foreground/30";
  if (isFm && fmRejected) return "bg-error/80 border-error";
  if (isFm) return "bg-success/80 border-success";
  return "bg-primary/60 border-primary/80";
}

export function AgentCascadeStrip(props: AgentCascadeStripProps) {
  const { userId, decisionId, fmRejected, onNodeClick } = props;
  const [rows, setRows] = useState<AgentActivityRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!decisionId) {
      // W10 — Option 1 from the cleanup brief: only call setState
      // when the value actually changes, so the reset is a no-op for
      // the canonical first-render path (rows is already []). Using
      // the functional form lets us read the latest committed value
      // without re-subscribing the effect to ``rows``. This still
      // exists inside the effect, but it now triggers a render only
      // when an in-flight fetch had previously populated rows for a
      // now-stale decisionId — the legitimate "reset on prop change"
      // use case the React docs explicitly allow.
      // eslint-disable-next-line react-hooks/set-state-in-effect -- justified: prop-driven reset of stale fetch results (see comment).
      setRows((prev) => (prev.length === 0 ? prev : []));
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .agentActivity(userId, 500, { detail: false, decisionId })
      .then((data) => {
        if (!cancelled) setRows(data.rows);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [decisionId, userId]);

  const roleSet = useMemo(() => {
    return new Set(rows.map((r) => r.agent_role));
  }, [rows]);

  // For decision tokens like "plan-synth-19", strip "plan-synth-" to get
  // the int decision_run_id used by /decisions/[id].
  const replayHref = useMemo(() => {
    if (!decisionId) return null;
    const m = decisionId.match(/(\d+)$/);
    return m ? `/decisions/${m[1]}` : null;
  }, [decisionId]);

  if (!decisionId) return null;

  return (
    <div className="rounded-md border border-border/60 bg-muted/10 p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold tracking-wide uppercase text-muted-foreground">
          Agent cascade
        </h3>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="font-mono">{decisionId}</span>
          {replayHref && (
            <Link
              href={replayHref}
              className="text-primary hover:underline"
            >
              View full replay →
            </Link>
          )}
        </div>
      </div>

      {loading && (
        <p className="text-xs text-muted-foreground">Loading…</p>
      )}
      {error && (
        <p className="text-xs text-error font-mono">{error}</p>
      )}

      {!loading && !error && (
        <div className="flex flex-wrap items-start gap-4">
          {PHASES.map((phase, pi) => (
            <div key={phase.label} className="flex flex-col gap-1 min-w-0">
              <span className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
                {phase.label}
              </span>
              <div className="flex flex-wrap gap-1.5">
                {phase.roles.map((role) => {
                  const present = roleSet.has(role);
                  const isFm = role === "fund_manager";
                  return (
                    <button
                      key={role}
                      type="button"
                      title={`${role}${present ? "" : " — not in this run"}`}
                      onClick={() => present && onNodeClick?.(role)}
                      disabled={!present}
                      className={`h-7 rounded-full border px-2 text-[10px] font-mono ${nodeColor(present, isFm, fmRejected)} ${
                        present
                          ? "cursor-pointer hover:brightness-110"
                          : "cursor-default opacity-60"
                      } text-white`}
                    >
                      {role}
                    </button>
                  );
                })}
              </div>
              {pi < PHASES.length - 1 && (
                <span aria-hidden className="hidden md:block self-center" />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
