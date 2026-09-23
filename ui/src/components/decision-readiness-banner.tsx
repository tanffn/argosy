"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

type Job = {
  name: string; status: string; last_success_at?: string | null; last_attempt_at?: string | null;
  attention_required?: boolean;
  failure?: { reason: string; action: string } | null;
};
export type KnowledgeDocument = {
  path: string; state: string; input_matches?: boolean; checked_at?: string; report_id?: number; note: string; review_reason?: string;
  findings: { scope: string; claim: string; missing_evidence: string; owner: string; next_action: string; affected_advice: string; retry_after?: string | null; urgency?: "routine" | "urgent" }[];
};
type Readiness = { status: string; message: string; checked_at?: string; pending_news?: number; jobs?: Job[];
  urgent_findings?: (KnowledgeDocument["findings"][number] & { path: string })[];
  knowledge?: { status: string; total?: number; verified?: number; reported_verified?: number; outstanding?: number;
    reason?: string; paperwork_due_date?: string | null; documents: KnowledgeDocument[] } | null;
};

function timestamp(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "No successful run recorded";
}

export function knowledgeLabel(state: string) {
  if (state === "needs_review" || state === "not_reviewed") return "Argosy review pending";
  if (state === "incomplete") return "Reviewed — specific evidence still needed";
  return state.replaceAll("_", " ");
}

export function useDecisionReadiness(userId = "ariel") {
  const [state, setState] = useState<Readiness | null>(null);
  useEffect(() => {
    let disposed = false;
    let controller: AbortController | null = null;
    const refresh = async () => {
      if (controller) return;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 10_000);
      try {
        const res = await fetch(`/api/health/decisions?user_id=${encodeURIComponent(userId)}`, { cache: "no-store", signal: controller.signal });
        if (!res.ok) throw new Error("Readiness unavailable");
        const next: Readiness = await res.json();
        if (!["ready", "blocked", "degraded", "unknown"].includes(next.status) || typeof next.message !== "string") {
          throw new Error("Invalid readiness response");
        }
        if (!disposed) setState(next);
      } catch {
        if (!disposed) setState({ status: "unknown", message: "Cannot reach or read Argosy's analysis status. Check the backend service; recommendations may be stale." });
      } finally {
        clearTimeout(timeout);
        controller = null;
      }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 60_000);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => { disposed = true; controller?.abort(); clearInterval(timer); window.removeEventListener("focus", onFocus); };
  }, [userId]);
  return state;
}

export function DecisionReadinessBanner() {
  const state = useDecisionReadiness();
  const healthy = state?.status === "ready";
  const failed = state?.status === "blocked" || state?.status === "unknown" || state?.jobs?.some((job) => job.status === "error");
  const problems = state?.jobs?.filter((job) => job.status !== "knowledge_incomplete" && (job.attention_required ?? !["ok", "disabled", "knowledge_recovered"].includes(job.status))) ?? [];
  const disabled = state?.jobs?.filter((job) => job.status === "disabled") ?? [];
  const actions = [...new Set(problems.flatMap((job) => job.failure ? [job.failure.action] : []))];
  const title = !state ? "Checking status" : healthy ? "Analysis up to date" : state.status === "blocked" ? "Action required" : state.status === "unknown" ? "Status unavailable" : "Analysis incomplete";
  return (
    <aside aria-label="Argosy operational status" role={failed ? "alert" : "status"} className={`border-b px-6 py-3 text-sm ${healthy ? "border-emerald-500/40 bg-emerald-500/10" : failed ? "border-red-500/40 bg-red-500/10" : "border-amber-500/40 bg-amber-500/10"}`}>
      <div className="mx-auto max-w-6xl">
        <strong>Argosy — {title}.</strong> {state?.message ?? "Verifying recorded job outcomes, not just whether the server is online."}
        {actions.map((action) => <p key={action} className="mt-2 font-medium">{action}</p>)}
        {!!state?.pending_news && <p className="mt-1">{state.pending_news} news items awaiting analysis.</p>}
        {state?.urgent_findings?.map((finding, i) => <div key={`${finding.path}:${i}`} className="mt-2">
          <p className="font-medium">{finding.claim}</p>
          <p>{finding.affected_advice}</p><p>Next action: {finding.next_action}</p>
        </div>)}
        {problems.length > 0 && (
          <details className="mt-2">
            <summary className="cursor-pointer font-medium">{problems.length} jobs need attention — failures and last successful runs</summary>
          <ul className="mt-2 space-y-1">
            {problems.map((job) => (
              <li key={job.name}>
                <span className="font-medium">{job.name.replaceAll("_", " ")}: {job.status.replaceAll("_", " ")}</span>
                {job.failure && <> — {job.failure.reason}</>}
                <span className="block text-xs text-muted-foreground">Last success: {timestamp(job.last_success_at)}{job.last_attempt_at && <> · Last attempt: {timestamp(job.last_attempt_at)}</>}</span>
              </li>
            ))}
          </ul>
          </details>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
          {disabled.length > 0 && <span>Disabled by configuration: {disabled.map((job) => job.name.replaceAll("_", " ")).join(", ")}. Historical failures remain in Job history.</span>}
          <Link href="/jobs" className="underline">View job history / retry</Link>
          {state?.checked_at && <span>Checked: {timestamp(state.checked_at)} · Refreshes every minute</span>}
        </div>
      </div>
    </aside>
  );
}
