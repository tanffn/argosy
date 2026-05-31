"use client";

/**
 * DecisionAccordion — live agent-cascade visibility on the home page.
 *
 * Renders one collapsed row per decision group from useDecisionStream.
 * Expanding a row shows a vertical stack of AgentRunCard for each agent run
 * in that decision, ordered by started_at asc (the hook already sorts them).
 *
 * Clicking any AgentRunCard opens AgentDetailDrawer for that agent run.
 * WS-only rows (id === null) are rendered with a disabled visual and no-op
 * onSelect — the drawer requires a persisted DB row.
 *
 * In-progress decisions pulse their border via Tailwind animate-pulse /
 * border-info. Finished decisions use a standard border.
 *
 * Tier and ticker are surfaced from /api/decisions/recent (Task 8 follow-up).
 * WS-only entries (not yet persisted) render null for both fields.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  useDecisionStream,
  type AgentRow,
  type DecisionGroup,
} from "@/lib/useDecisionStream";
import type { AgentActivityRow } from "@/lib/api";
import { AgentRunCard } from "@/components/agent/AgentRunCard";
import { AgentDetailDrawer } from "@/components/agent/AgentDetailDrawer";
import { StatusPill } from "@/components/ui/status-pill";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type DecisionAccordionProps = {
  userId: string;
};

// ---------------------------------------------------------------------------
// Adapters
// ---------------------------------------------------------------------------

/**
 * Convert an AgentRow (from useDecisionStream) to an AgentActivityRow
 * so it can be passed to AgentRunCard and AgentDetailDrawer, which
 * expect the REST-shaped type. WS-only rows (id === null) are coerced to
 * id = -1; callers must guard against opening the drawer for those.
 */
function agentRowToActivityRow(r: AgentRow): AgentActivityRow {
  return {
    id: r.id ?? -1,
    user_id: r.user_id,
    agent_role: r.agent_role,
    decision_id: r.decision_id,
    intake_session_id: r.intake_session_id,
    model: r.model,
    confidence: r.confidence,
    tokens_in: r.tokens_in,
    tokens_out: r.tokens_out,
    cost_usd: r.cost_usd,
    created_at: r.created_at,
    cache_input_tokens: r.cache_input_tokens,
    cache_creation_tokens: r.cache_creation_tokens,
    thinking_tokens: r.thinking_tokens,
    citations_count: r.citations_count,
    response_text: r.response_text,
    citations_json: r.citations_json,
    prompt_hash: r.prompt_hash,
    // Wave B-UI Task 9 — WS stubs never carry sources; default to empty.
    sources_preview: r.sources_preview ?? [],
    // Wave B-UI follow-up Item 2 — pass through for O(1) WS↔DB linking.
    run_correlation_id: r.run_correlation_id ?? null,
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}

// USD cost intentionally not surfaced in the decisions list — see the
// no-USD-reporting binding preference. The detail drawer per agent still
// shows it for inspection.

function statusTone(
  status: DecisionGroup["status"],
): "success" | "warning" | "error" | "neutral" {
  if (status === "done") return "success";
  if (status === "failed") return "error";
  return "neutral"; // running — the pulsing border handles in-progress visual
}

// T4.4 — synthesis-family runs (plan_revision, delta_pushback, daily_brief)
// stamp ticker = "(plan)" because the DB column is NOT NULL but those flows
// aren't ticker-scoped. Treat the sentinel as "no real ticker" for display.
function hasRealTicker(ticker: string | null): boolean {
  return !!ticker && ticker !== "(plan)";
}

// T4.4 — extract a kind-appropriate label from DecisionRun.notes_json.
// Returns null when the kind isn't one of the new T4.4 kinds OR when the
// notes blob is missing / malformed. The accordion row always falls back
// to the raw decision_kind label when this returns null, so a parse miss
// is non-fatal.
//
// Schemas (defined here so the renderer is self-documenting):
//   delta_pushback -> { "delta_item_id": "<string>", ... }
//   daily_brief    -> { "brief_date": "YYYY-MM-DD", ... }
function kindLabel(
  decisionKind: string | null,
  notesJson: string | null | undefined,
): string | null {
  if (!decisionKind || !notesJson) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(notesJson);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const obj = parsed as Record<string, unknown>;
  if (decisionKind === "delta_pushback") {
    const itemId = obj.delta_item_id;
    if (typeof itemId === "string" && itemId.length > 0) {
      return `pushback · ${itemId}`;
    }
    return "pushback";
  }
  if (decisionKind === "daily_brief") {
    const date = obj.brief_date;
    if (typeof date === "string" && date.length > 0) {
      return `brief · ${date}`;
    }
    return "brief";
  }
  return null;
}

// ---------------------------------------------------------------------------
// DecisionRow — one collapsed/expanded decision entry
// ---------------------------------------------------------------------------

type DecisionRowProps = {
  group: DecisionGroup;
  expanded: boolean;
  onToggle: () => void;
  onSelectRun: (row: AgentActivityRow) => void;
};

function DecisionRow({
  group,
  expanded,
  onToggle,
  onSelectRun,
}: DecisionRowProps) {
  const isRunning = group.status === "running";

  return (
    <div
      className={cn(
        "rounded-lg border bg-card transition-colors duration-150",
        isRunning
          ? "border-info animate-pulse"
          : "border-border",
      )}
    >
      {/* Collapsed header row */}
      <button
        type="button"
        onClick={onToggle}
        className={cn(
          "w-full text-left px-4 py-3 flex items-center gap-3",
          "hover:bg-secondary/30 transition-colors duration-150",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          "rounded-lg",
        )}
        aria-expanded={expanded}
      >
        {/* Chevron */}
        <span className="shrink-0 text-muted-foreground">
          {expanded ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
        </span>

        {/* Timestamp */}
        <span className="font-mono text-xs text-muted-foreground shrink-0 w-40 tabular-nums">
          {formatTimestamp(group.startedAt)}
        </span>

        {/* Status pill */}
        <StatusPill tone={statusTone(group.status)}>
          {group.status}
        </StatusPill>

        {/* Agent count */}
        <span className="font-mono text-xs text-muted-foreground shrink-0">
          {group.rows.length} agent{group.rows.length !== 1 ? "s" : ""}
        </span>

        {/* Ticker — shown when present (trade_proposal / speculative runs).
            T4.4: "(plan)" sentinel suppressed so kind-specific label shows
            instead for synthesis-family runs. */}
        {hasRealTicker(group.ticker) && (
          <span className="font-mono text-xs font-semibold text-foreground shrink-0 uppercase">
            {group.ticker}
          </span>
        )}

        {/* Tier — shown when present */}
        {group.tier && (
          <span className="font-mono text-[10px] uppercase text-muted-foreground shrink-0 bg-secondary/60 px-1.5 py-0.5 rounded">
            {group.tier}
          </span>
        )}

        {/* decision_kind — shown as muted label when no ticker (e.g.
            plan_revision / delta_pushback / daily_brief runs). T4.4: for
            new kinds (delta_pushback, daily_brief) we surface a
            kind-specific summary (delta item id, brief date) parsed from
            notes_json. Falls back to the raw decision_kind if notes_json
            is missing / malformed. */}
        {!hasRealTicker(group.ticker) && group.decision_kind && (
          <span
            className="font-mono text-[10px] text-muted-foreground/70 shrink-0 truncate max-w-[14rem]"
            title={group.decision_kind}
          >
            {kindLabel(group.decision_kind, group.notes_json) ?? group.decision_kind}
          </span>
        )}

        {/* Spacer */}
        <span className="flex-1" />

        {/* Total duration */}
        <span className="font-mono text-xs text-muted-foreground shrink-0 w-14 text-right tabular-nums">
          {isRunning ? (
            <span className="italic">running</span>
          ) : (
            formatDuration(group.totalDurationMs)
          )}
        </span>

        {/* Decision key (short, truncated) */}
        <span
          className="font-mono text-[10px] text-muted-foreground/60 shrink-0 w-20 truncate text-right"
          title={group.key}
        >
          {group.key === "Standalone" ? "standalone" : group.key.slice(-8)}
        </span>
      </button>

      {/* Expanded: stack of AgentRunCard */}
      {expanded && (
        <div className="px-4 pb-3 flex flex-col gap-2">
          {group.rows.map((agentRow, i) => {
            const activityRow = agentRowToActivityRow(agentRow);
            const isWsOnly = agentRow.id === null;
            return (
              <div
                key={agentRow.run_correlation_id ?? `row-${i}`}
                className={cn(isWsOnly && "opacity-60 cursor-not-allowed")}
                title={isWsOnly ? "Awaiting DB flush — details unavailable" : undefined}
              >
                <AgentRunCard
                  row={activityRow}
                  status={agentRow.status}
                  durationMs={agentRow.durationMs}
                  onSelect={
                    isWsOnly
                      ? () => undefined
                      : () => onSelectRun(activityRow)
                  }
                />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// DecisionAccordion — public component
// ---------------------------------------------------------------------------

export function DecisionAccordion({ userId }: DecisionAccordionProps) {
  const { decisions, isLoading } = useDecisionStream(userId);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selectedRow, setSelectedRow] = useState<AgentActivityRow | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  function toggleGroup(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  function handleSelectRun(row: AgentActivityRow) {
    setSelectedRow(row);
    setDrawerOpen(true);
  }

  if (isLoading) {
    return (
      <div className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-xs text-muted-foreground font-mono">
        Loading agent activity…
      </div>
    );
  }

  if (decisions.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-xs text-muted-foreground font-mono">
        No agent runs yet.
      </div>
    );
  }

  return (
    <>
      <div className="flex flex-col gap-2">
        {decisions.map((group) => (
          <DecisionRow
            key={group.key}
            group={group}
            expanded={expanded.has(group.key)}
            onToggle={() => toggleGroup(group.key)}
            onSelectRun={handleSelectRun}
          />
        ))}
      </div>

      <AgentDetailDrawer
        row={selectedRow}
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
      />
    </>
  );
}
