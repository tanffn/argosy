"use client";

/**
 * AgentCascadePanel — live agent-cascade visibility panel for the advisor page.
 *
 * While a turn is in-flight (turnId set, isResolved=false) it renders a
 * vertical stack of AgentRunCard rows filtered to the current turn_id, with
 * auto-scroll on new rows.
 *
 * When isResolved=true and there are rows: collapses to a one-line summary
 * with a [view detail] toggle that re-expands the list.
 *
 * Clicking any row opens the AgentDetailDrawer for that row (WS-only rows
 * with id===null show a "(detail loads once persisted)" tooltip and are
 * not clickable).
 */

import { useEffect, useRef, useState } from "react";
import { useDecisionStream, type AgentRow } from "@/lib/useDecisionStream";
import { AgentRunCard } from "@/components/agent/AgentRunCard";
import { AgentDetailDrawer } from "@/components/agent/AgentDetailDrawer";
import type { AgentActivityRow } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type AgentCascadePanelProps = {
  userId: string;
  /** null when no turn is in flight. Keep set after POST resolves so the
   *  panel stays visible; reset at the top of the NEXT call to askNext. */
  turnId: string | null;
  /** true once api.advisorTurn() has returned (either success or error). */
  isResolved: boolean;
  /** Backend-status / last-agent-step diagnostic line, visually subordinated. */
  diagnosticLine?: React.ReactNode;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a total cost in USD with 4 decimal places. */
function formatCost(usd: number): string {
  return `$${usd.toFixed(4)}`;
}

/** Format a duration from milliseconds to seconds with 1 decimal place. */
function formatDurationS(ms: number | null): string {
  if (ms === null) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Convert an AgentRow (from useDecisionStream) to an AgentActivityRow shape
 * that AgentRunCard + AgentDetailDrawer accept.
 *
 * AgentRow is a superset of AgentActivityRow with id widened to number|null.
 * For AgentActivityRow consumers that require id:number we only cast when
 * we know id !== null (i.e. when opening the drawer).
 */
function agentRowToActivityRow(row: AgentRow): AgentActivityRow {
  return {
    id: row.id ?? -1, // -1 is a sentinel for WS-only rows; drawer guards on this
    user_id: row.user_id,
    agent_role: row.agent_role,
    decision_id: row.decision_id,
    intake_session_id: row.intake_session_id,
    model: row.model,
    confidence: row.confidence,
    tokens_in: row.tokens_in,
    tokens_out: row.tokens_out,
    cost_usd: row.cost_usd,
    created_at: row.created_at,
    cache_input_tokens: row.cache_input_tokens,
    cache_creation_tokens: row.cache_creation_tokens,
    thinking_tokens: row.thinking_tokens,
    citations_count: row.citations_count,
    response_text: row.response_text,
    citations_json: row.citations_json,
    prompt_hash: row.prompt_hash,
    // Wave B-UI Task 9 — WS stubs never carry sources; default to empty.
    sources_preview: row.sources_preview ?? [],
    // Wave B-UI follow-up Item 2 — pass through for O(1) WS↔DB linking.
    run_correlation_id: row.run_correlation_id ?? null,
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function AgentCascadePanel({
  userId,
  turnId,
  isResolved,
  diagnosticLine,
}: AgentCascadePanelProps) {
  const { decisions } = useDecisionStream(userId, {
    turnId: turnId ?? undefined,
  });

  // Flatten all rows from all matching decisions into a single list.
  const allRows: AgentRow[] = decisions.flatMap((d) => d.rows);

  // Aggregate cost + duration across all decisions for the summary line.
  const totalCostUsd = decisions.reduce((acc, d) => acc + d.totalCostUsd, 0);
  const totalDurationMs = (() => {
    const durations = decisions.map((d) => d.totalDurationMs);
    if (durations.some((d) => d === null)) return null;
    return durations.reduce<number>((acc, d) => acc + d!, 0);
  })();

  // Collapsed/expanded state for the post-resolution summary.
  const [expanded, setExpanded] = useState(true);

  // Re-expand whenever a new turn starts (turnId changes to non-null).
  useEffect(() => {
    if (turnId !== null) setExpanded(true);
  }, [turnId]);

  // Drawer state.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedRow, setSelectedRow] = useState<AgentActivityRow | null>(null);

  // Auto-scroll container ref.
  const listRef = useRef<HTMLDivElement>(null);
  const rowCount = allRows.length;
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [rowCount]);

  // Nothing to show when no turnId and no rows.
  if (turnId === null && allRows.length === 0) return null;

  const summaryText = (() => {
    const agentWord = allRows.length === 1 ? "agent" : "agents";
    const costStr = formatCost(totalCostUsd);
    const durStr = formatDurationS(totalDurationMs);
    if (isResolved) {
      return `Cascade complete: ${allRows.length} ${agentWord} · ${costStr} · ${durStr}`;
    }
    return `Cascade — ${allRows.length} ${agentWord} · ${costStr} · ${durStr}`;
  })();

  return (
    <>
      <div
        className="rounded-md border border-border bg-secondary/20 text-xs font-mono"
        aria-live="polite"
      >
        {/* Header row */}
        <div className="flex items-center justify-between px-3 py-2 border-b border-border/60">
          <span className="text-sm font-medium text-foreground">
            {summaryText}
          </span>
          {isResolved && allRows.length > 0 && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="text-xs text-primary hover:underline underline-offset-4 ml-3 shrink-0"
            >
              {expanded ? "collapse" : "view detail"}
            </button>
          )}
        </div>

        {/* Agent row list — shown while in-flight OR when expanded after resolution */}
        {(expanded || !isResolved) && allRows.length > 0 && (
          <div
            ref={listRef}
            className="flex flex-col gap-1 p-2 max-h-72 overflow-y-auto"
          >
            {allRows.map((row, idx) => {
              const activityRow = agentRowToActivityRow(row);
              const isPersisted = row.id !== null && row.id !== -1;
              const key = row.run_correlation_id ?? `row-${idx}`;

              if (!isPersisted) {
                // WS-only row: show the card but disable click with a tooltip.
                return (
                  <div
                    key={key}
                    title="(detail loads once persisted)"
                    className="opacity-70 cursor-not-allowed"
                  >
                    <AgentRunCard
                      row={activityRow}
                      status={row.status}
                      durationMs={row.durationMs}
                      onSelect={() => {
                        /* not clickable until persisted */
                      }}
                    />
                  </div>
                );
              }

              return (
                <AgentRunCard
                  key={key}
                  row={activityRow}
                  status={row.status}
                  durationMs={row.durationMs}
                  onSelect={() => {
                    setSelectedRow(activityRow);
                    setDrawerOpen(true);
                  }}
                />
              );
            })}
          </div>
        )}

        {/* Diagnostic line — visually subordinated at the bottom */}
        {diagnosticLine && (
          <div className="px-3 py-1.5 border-t border-border/40 text-[11px] text-muted-foreground">
            {diagnosticLine}
          </div>
        )}
      </div>

      {/* Detail drawer */}
      <AgentDetailDrawer
        row={selectedRow}
        open={drawerOpen}
        onOpenChange={(open) => {
          setDrawerOpen(open);
          if (!open) setSelectedRow(null);
        }}
      />
    </>
  );
}
