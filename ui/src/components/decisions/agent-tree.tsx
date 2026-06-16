"use client";

import { useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  MinusCircle,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";

import type {
  AgentNode,
  AgentNodeStatus,
  CodexFinding,
  CodexFindingSeverity,
  CoherenceFinding,
  CodexReconcileMarker,
  HeadlineAuditRow,
} from "@/lib/api";

import { AdapterLeaf } from "./adapter-leaf";

// T0.6 — recursive FM-rooted DAG view. Reads AgentTreeResponse.root
// from GET /api/decisions/{id}/agent-tree and replaces the old
// "Sequence (full run)" mermaid diagram (which only showed phase
// boundaries, not who-talked-to-whom).
//
// Key handling: the backend dedups nodes by agent_report_id, but the
// same upstream node can appear under multiple parents in the rendered
// DAG (e.g. fundamentals analyst feeds both bull and bear). React keys
// only need to be unique among *siblings*, so a stable per-parent key
// combining agent_report_id (when present) with the sibling index is
// sufficient and avoids the "duplicate key" warning that arises if we
// keyed only on agent_report_id.

const STATUS_ICON: Record<AgentNodeStatus, typeof CheckCircle2> = {
  ok: CheckCircle2,
  degraded: MinusCircle,
  failed: AlertCircle,
  skipped: AlertCircle,
};

const STATUS_COLOR: Record<AgentNodeStatus, string> = {
  ok: "text-success",
  degraded: "text-warning",
  failed: "text-error",
  skipped: "text-muted-foreground",
};

// Codex finding severities map to the same Tailwind tokens the adapter
// leaf + analyst status icons use. BLOCKER ≈ failed (error), AMBER ≈
// degraded (warning), YELLOW ≈ a softer warning (muted foreground keeps
// it visually less alarming than AMBER).
const CODEX_SEVERITY_COLOR: Record<CodexFindingSeverity, string> = {
  BLOCKER: "text-error",
  AMBER: "text-warning",
  YELLOW: "text-muted-foreground",
};

function siblingKey(child: AgentNode, index: number): string {
  return child.agent_report_id !== null
    ? `r${child.agent_report_id}#${index}`
    : `idx${index}-${child.agent_role}`;
}

export function AgentTree({ root }: { root: AgentNode }) {
  return (
    <div className="font-mono text-xs">
      <AgentTreeNode node={root} depth={0} />
    </div>
  );
}

function AgentTreeNode({ node, depth }: { node: AgentNode; depth: number }) {
  // Open FM (depth 0) and its direct children (depth 1) by default so
  // the first useful frame fits on screen; deeper layers stay collapsed
  // to avoid an overwhelming wall of analyst rows.
  const [open, setOpen] = useState(depth < 1);
  const StatusIcon = STATUS_ICON[node.status];
  // codex_second_opinion is a cross-engine leaf — its findings act as
  // "children" for the disclosure caret even though the codex node has
  // no AgentNode children.
  const isCodex = node.agent_role === "codex_second_opinion";
  // whole_artifact_reader is the holistic final-stage coherence pass — a
  // cross-engine leaf like codex, but its findings have a different shape
  // (CoherenceFinding). Its findings act as disclosure "children" too.
  const isWholeArtifactReader = node.agent_role === "whole_artifact_reader";
  const headlineAudit = node.headline_audit ?? [];
  const reconcile = node.reconcile ?? null;
  const hasChildren =
    node.children.length > 0 ||
    node.adapters.length > 0 ||
    (isCodex && node.codex_findings.length > 0) ||
    (isCodex && headlineAudit.length > 0) ||
    ((isCodex || isWholeArtifactReader) && reconcile !== null) ||
    (isWholeArtifactReader && node.coherence_findings.length > 0);
  return (
    <div className="border-l border-border ml-2">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 px-2 py-1 hover:bg-secondary/40 w-full text-left"
        aria-expanded={open}
      >
        {hasChildren ? (
          open ? (
            <ChevronDown
              className="h-3 w-3 shrink-0"
              aria-hidden
              suppressHydrationWarning
            />
          ) : (
            <ChevronRight
              className="h-3 w-3 shrink-0"
              aria-hidden
              suppressHydrationWarning
            />
          )
        ) : (
          <span className="w-3" />
        )}
        <StatusIcon
          className={`h-3 w-3 shrink-0 ${STATUS_COLOR[node.status]}`}
          aria-hidden
          suppressHydrationWarning
        />
        <span className="font-semibold">{node.agent_role}</span>
        {isCodex && (
          // Small badge to make it obvious at a glance that this node
          // is a CROSS-ENGINE second opinion (codex/gpt-5), not one of
          // Argosy's native Claude analysts.
          <span
            className="text-[9px] px-1.5 py-0.5 rounded-full bg-accent text-accent-foreground font-bold uppercase tracking-wider"
            title="Cross-engine second opinion via OpenAI gpt-5 (codex-tandem kit)"
          >
            gpt-5
          </span>
        )}
        {isWholeArtifactReader && (
          // Badge to flag this node as the holistic whole-artifact
          // coherence reader (reads the assembled plan AS A WHOLE), not
          // one of Argosy's per-section analysts.
          <span
            className="text-[9px] px-1.5 py-0.5 rounded-full bg-accent text-accent-foreground font-bold uppercase tracking-wider"
            title="Whole-artifact adversarial reader — holistic coherence pass over the assembled plan"
          >
            whole-artifact
          </span>
        )}
        {node.side && (
          <span className="text-muted-foreground">({node.side})</span>
        )}
        {node.perspective && (
          <span className="text-muted-foreground">({node.perspective})</span>
        )}
        {node.confidence && (
          <span className="text-[10px] px-1 rounded bg-muted">
            {node.confidence}
          </span>
        )}
        {node.model && (
          <span className="text-[10px] text-muted-foreground">
            {node.model}
          </span>
        )}
        {node.thinking_tokens !== null && node.thinking_tokens > 0 && (
          // Adaptive-thinking telemetry: how many tokens the model
          // actually spent thinking on this call. Hidden when 0 / null
          // to avoid clutter on agents that don't think (effort="low"
          // / categorizer / etc.). FM at effort="max" is the most
          // useful surface — surfaces "verdict cost N thinking tokens"
          // for effort-level tuning.
          <span
            className="text-[10px] text-muted-foreground"
            title="Adaptive thinking tokens used on this call"
          >
            {node.thinking_tokens.toLocaleString()} thinking
          </span>
        )}
        {node.cost_usd !== null && (
          <span className="ml-auto text-muted-foreground">
            ${node.cost_usd.toFixed(4)}
          </span>
        )}
      </button>
      {open && (
        <div className="pl-4">
          {node.failure_reason && (
            <div className="px-2 py-1 text-error text-[11px]">
              {node.failure_reason}
            </div>
          )}
          {node.response_excerpt && (
            <details className="px-2 py-1">
              <summary className="cursor-pointer text-muted-foreground">
                response (first 500 chars)
              </summary>
              <pre className="whitespace-pre-wrap text-[11px] pt-1">
                {node.response_excerpt}
              </pre>
            </details>
          )}
          {(isCodex || isWholeArtifactReader) && reconcile !== null && (
            <ReconcileBanner
              marker={reconcile}
              label={isWholeArtifactReader ? "reader" : "codex"}
            />
          )}
          {isCodex && headlineAudit.length > 0 && (
            <HeadlineAuditTable rows={headlineAudit} />
          )}
          {isCodex &&
            node.codex_findings.map((f, i) => (
              <CodexFindingRow
                key={`codex-finding-${i}-${f.severity}-${f.topic}`}
                finding={f}
              />
            ))}
          {isWholeArtifactReader &&
            node.coherence_findings.map((f, i) => (
              <CoherenceFindingRow
                key={`coherence-finding-${i}-${f.severity}-${f.kind}`}
                finding={f}
              />
            ))}
          {node.adapters.map((a, i) => (
            <AdapterLeaf
              key={`${a.adapter_name}-${a.target ?? "_"}-${i}`}
              adapter={a}
            />
          ))}
          {node.children.map((c, i) => (
            <AgentTreeNode
              key={siblingKey(c, i)}
              node={c}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// One parsed CodexFinding rendered as an expandable sub-row under the
// codex_second_opinion node. Severity icon left, topic + detail right,
// optional suggested-fix in a collapsible <details>.
function CodexFindingRow({ finding }: { finding: CodexFinding }) {
  const color = CODEX_SEVERITY_COLOR[finding.severity];
  return (
    <div className="border-l border-border ml-2 px-2 py-1 text-[11px]">
      <div className="flex items-center gap-2">
        <ShieldAlert
          className={`h-3 w-3 shrink-0 ${color}`}
          aria-hidden
          suppressHydrationWarning
        />
        <span className={`font-semibold ${color}`}>{finding.severity}</span>
        <span className="font-mono">{finding.topic}</span>
      </div>
      {finding.detail && (
        <div className="pl-5 pt-0.5 text-muted-foreground whitespace-pre-wrap">
          {finding.detail}
        </div>
      )}
      {finding.suggested_fix && (
        <details className="pl-5 pt-0.5">
          <summary className="cursor-pointer text-muted-foreground">
            suggested fix
          </summary>
          <div className="pt-0.5 whitespace-pre-wrap">
            {finding.suggested_fix}
          </div>
        </details>
      )}
    </div>
  );
}

// Audit-row status -> color. DIVERGES / UNVERIFIABLE are the visible
// "codex re-derived a different number" signal and render in red (error);
// MATCH is muted/ok green so the eye goes straight to the divergences.
const AUDIT_STATUS_COLOR: Record<string, string> = {
  MATCH: "text-success",
  DIVERGES: "text-error",
  UNVERIFIABLE: "text-error",
};

function fmtAuditValue(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  // Headline numbers can be large (net worth in NIS) or fractional
  // (weight pct). Group thousands; keep it compact.
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

// The codex re-derivation AUDIT — the concrete proof of the adversarial
// pushback. Each row shows codex's independent figure next to the
// pipeline's claim and the verdict; DIVERGES / UNVERIFIABLE render in red.
function HeadlineAuditTable({ rows }: { rows: HeadlineAuditRow[] }) {
  const diverging = rows.filter(
    (r) => r.status === "DIVERGES" || r.status === "UNVERIFIABLE",
  ).length;
  return (
    <div className="border-l border-border ml-2 px-2 py-1 text-[11px]">
      <div className="flex items-center gap-2 pb-1">
        <ShieldAlert
          className={`h-3 w-3 shrink-0 ${diverging > 0 ? "text-error" : "text-success"}`}
          aria-hidden
          suppressHydrationWarning
        />
        <span className="font-semibold">
          re-derivation audit ({rows.length} metric
          {rows.length === 1 ? "" : "s"}
          {diverging > 0 ? `, ${diverging} diverge/unverifiable` : ""})
        </span>
      </div>
      <div className="pl-5 flex flex-col gap-1">
        {rows.map((r, i) => {
          const color = AUDIT_STATUS_COLOR[r.status] ?? "text-muted-foreground";
          return (
            <div
              key={`audit-${i}-${r.metric}-${r.status}`}
              className="flex flex-col"
            >
              <div className="flex items-center gap-2 flex-wrap">
                <span className={`font-semibold ${color}`}>{r.status}</span>
                <span className="font-mono">{r.metric}</span>
                <span className="text-muted-foreground">
                  independent{" "}
                  <span className="font-mono text-foreground">
                    {fmtAuditValue(r.independent_value)}
                  </span>{" "}
                  vs claimed{" "}
                  <span className="font-mono text-foreground">
                    {fmtAuditValue(r.claimed_value)}
                  </span>
                </span>
              </div>
              {(r.formula || r.raw_rows_used.length > 0) && (
                <details className="pl-4 pt-0.5">
                  <summary className="cursor-pointer text-muted-foreground">
                    derivation
                  </summary>
                  {r.formula && (
                    <div className="pt-0.5 whitespace-pre-wrap">{r.formula}</div>
                  )}
                  {r.raw_rows_used.length > 0 && (
                    <ul className="pt-0.5 list-disc pl-4">
                      {r.raw_rows_used.map((row, j) => (
                        <li
                          key={`audit-${i}-row-${j}`}
                          className="whitespace-pre-wrap"
                        >
                          {row}
                        </li>
                      ))}
                    </ul>
                  )}
                </details>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// The visible "zigzag" reconcile element: the reviewer (codex at phase 4.5,
// or the whole-artifact reader at phase 5.5) pushed back -> the synthesizer
// was re-run to correct it -> {resolved | still blocking}. Renders amber when
// resolved (the loop worked) and red when the reviewer still blocks after the
// correction round (the pushback was NOT resolved).
function ReconcileBanner({
  marker,
  label = "codex",
}: {
  marker: CodexReconcileMarker;
  label?: string;
}) {
  const color = marker.still_blocking ? "text-error" : "text-warning";
  const outcome = marker.still_blocking
    ? "still blocking after re-synthesis"
    : "resolved after re-synthesis";
  return (
    <div className="border-l-2 border-border ml-2 px-2 py-1 text-[11px] bg-secondary/30">
      <div className="flex items-center gap-2 flex-wrap">
        <RefreshCw
          className={`h-3 w-3 shrink-0 ${color}`}
          aria-hidden
          suppressHydrationWarning
        />
        <span className={`font-semibold ${color}`}>{label} reconcile (zigzag)</span>
        <span className="text-muted-foreground">
          pushed back &rarr; re-synthesized &rarr;{" "}
          <span className={color}>{outcome}</span>
        </span>
      </div>
      {marker.objection_topic && (
        <div className="pl-5 pt-0.5 text-muted-foreground">
          objection: <span className="font-mono">{marker.objection_topic}</span>
        </div>
      )}
    </div>
  );
}

// One parsed CoherenceFinding rendered as an expandable sub-row under the
// whole_artifact_reader node. Severity icon left, kind + detail right,
// the verbatim conflicting surfaces in a collapsible <details>. Mirrors
// CodexFindingRow's styling; the finding shape differs (kind +
// surfaces_cited instead of topic + suggested_fix).
function CoherenceFindingRow({ finding }: { finding: CoherenceFinding }) {
  const color = CODEX_SEVERITY_COLOR[finding.severity];
  return (
    <div className="border-l border-border ml-2 px-2 py-1 text-[11px]">
      <div className="flex items-center gap-2">
        <ShieldAlert
          className={`h-3 w-3 shrink-0 ${color}`}
          aria-hidden
          suppressHydrationWarning
        />
        <span className={`font-semibold ${color}`}>{finding.severity}</span>
        <span className="font-mono">{finding.kind}</span>
      </div>
      {finding.detail && (
        <div className="pl-5 pt-0.5 text-muted-foreground whitespace-pre-wrap">
          {finding.detail}
        </div>
      )}
      {finding.surfaces_cited.length > 0 && (
        <details className="pl-5 pt-0.5">
          <summary className="cursor-pointer text-muted-foreground">
            surfaces cited ({finding.surfaces_cited.length})
          </summary>
          <ul className="pt-0.5 list-disc pl-4">
            {finding.surfaces_cited.map((s, i) => (
              <li key={`surface-${i}`} className="whitespace-pre-wrap">
                {s}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
