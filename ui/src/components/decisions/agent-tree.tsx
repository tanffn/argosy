"use client";

import { useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  MinusCircle,
} from "lucide-react";

import type { AgentNode, AgentNodeStatus } from "@/lib/api";

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
  const hasChildren = node.children.length > 0 || node.adapters.length > 0;
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
