"use client";

import { CheckCircle2, AlertTriangle, Ban, ShieldCheck } from "lucide-react";

interface DebateOutcomeJson {
  winning_side?: string;
  synthesis?: string;
  cited_evidence?: string[];
  rounds_run?: number;
  confidence?: string;
}
interface RiskOutcomeJson {
  consensus_verdict?: string;
  consolidated_conditions?: string[];
  dissent_summary?: string;
  rounds_run?: number;
  confidence?: string;
}
interface FundManagerDecisionJson {
  decision?: string;
  reason?: string;
  required_conditions?: string[];
  post_execution_checks?: string[];
  confidence?: string;
}
interface FundManagerPlanRevisionJson {
  approved?: boolean;
  reasons?: string[];
}

/**
 * Switch-on-verdict_kind renderer. For unknown verdict shapes, falls
 * back to a JSON pre-block so the data is at least visible.
 */
export function VerdictCard({
  verdictKind,
  verdict,
}: {
  verdictKind: string | null;
  verdict: Record<string, unknown> | null;
}) {
  if (!verdict || !verdictKind) {
    return (
      <p className="text-xs text-muted-foreground italic">
        No structured verdict for this phase.
      </p>
    );
  }

  if (verdictKind === "DebateOutcome") {
    const v = verdict as DebateOutcomeJson;
    const winner = v.winning_side ?? "?";
    const tone =
      winner === "bull"
        ? "text-success"
        : winner === "bear"
          ? "text-warning"
          : "text-muted-foreground";
    return (
      <div className="flex flex-col gap-2 text-xs font-mono">
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4" aria-hidden suppressHydrationWarning />
          <span>Debate winner:</span>
          <span className={`uppercase font-semibold ${tone}`}>{winner}</span>
          <span className="text-muted-foreground">
            · rounds {v.rounds_run ?? "?"} · {v.confidence ?? ""}
          </span>
        </div>
        {v.synthesis && (
          <p className="text-sm font-sans leading-snug">{v.synthesis}</p>
        )}
        {v.cited_evidence && v.cited_evidence.length > 0 && (
          <ul className="list-disc pl-5 text-xs">
            {v.cited_evidence.slice(0, 6).map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  if (verdictKind === "RiskOutcome") {
    const v = verdict as RiskOutcomeJson;
    const verdictWord = v.consensus_verdict ?? "?";
    const Icon =
      verdictWord === "APPROVE"
        ? CheckCircle2
        : verdictWord === "REJECT"
          ? Ban
          : AlertTriangle;
    const tone =
      verdictWord === "APPROVE"
        ? "text-success"
        : verdictWord === "REJECT"
          ? "text-error"
          : "text-warning";
    return (
      <div className="flex flex-col gap-2 text-xs font-mono">
        <div className="flex items-center gap-2">
          <Icon className={`h-4 w-4 ${tone}`} aria-hidden suppressHydrationWarning />
          <span>Risk verdict:</span>
          <span className={`uppercase font-semibold ${tone}`}>{verdictWord}</span>
          <span className="text-muted-foreground">
            · {v.confidence ?? ""}
          </span>
        </div>
        {v.consolidated_conditions && v.consolidated_conditions.length > 0 && (
          <div>
            <p className="text-xs text-muted-foreground">Conditions:</p>
            <ul className="list-disc pl-5 text-xs">
              {v.consolidated_conditions.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </div>
        )}
        {v.dissent_summary && (
          <p className="text-xs italic">
            <span className="text-muted-foreground">Dissent:</span>{" "}
            {v.dissent_summary}
          </p>
        )}
      </div>
    );
  }

  if (verdictKind === "FundManagerDecision") {
    const v = verdict as FundManagerDecisionJson;
    const decision = v.decision ?? "?";
    const Icon = decision === "green_light" ? CheckCircle2 : Ban;
    const tone =
      decision === "green_light" ? "text-success" : "text-error";
    return (
      <div className="flex flex-col gap-2 text-xs font-mono">
        <div className="flex items-center gap-2">
          <Icon className={`h-4 w-4 ${tone}`} aria-hidden suppressHydrationWarning />
          <span>FM:</span>
          <span className={`uppercase font-semibold ${tone}`}>{decision}</span>
          <span className="text-muted-foreground">
            · {v.confidence ?? ""}
          </span>
        </div>
        {v.reason && <p className="text-sm font-sans leading-snug">{v.reason}</p>}
        {v.required_conditions && v.required_conditions.length > 0 && (
          <div>
            <p className="text-xs text-muted-foreground">
              Required conditions:
            </p>
            <ul className="list-disc pl-5">
              {v.required_conditions.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </div>
        )}
        {v.post_execution_checks && v.post_execution_checks.length > 0 && (
          <div>
            <p className="text-xs text-muted-foreground">Post-execution:</p>
            <ul className="list-disc pl-5">
              {v.post_execution_checks.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    );
  }

  if (verdictKind === "FundManagerPlanRevisionDecision") {
    const v = verdict as FundManagerPlanRevisionJson;
    const Icon = v.approved ? CheckCircle2 : Ban;
    const tone = v.approved ? "text-success" : "text-error";
    return (
      <div className="flex flex-col gap-2 text-xs font-mono">
        <div className="flex items-center gap-2">
          <Icon className={`h-4 w-4 ${tone}`} aria-hidden suppressHydrationWarning />
          <span>Plan-revision:</span>
          <span className={`uppercase font-semibold ${tone}`}>
            {v.approved ? "APPROVED" : "REJECTED"}
          </span>
        </div>
        {v.reasons && v.reasons.length > 0 && (
          <ul className="list-disc pl-5 text-xs">
            {v.reasons.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  // Unknown verdict — show JSON.
  return (
    <pre className="text-xs font-mono bg-secondary/40 border border-border rounded-md p-2 overflow-auto">
      <code>{JSON.stringify(verdict, null, 2)}</code>
    </pre>
  );
}
