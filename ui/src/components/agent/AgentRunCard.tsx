"use client";

import { cn } from "@/lib/utils";
import { StatusPill } from "@/components/ui/status-pill";
import { type AgentActivityRow } from "@/lib/api";

type AgentRunCardProps = {
  row: AgentActivityRow;
  status: "running" | "done" | "failed";
  durationMs: number | null;
  onSelect: () => void;
};

function dotClass(
  status: AgentRunCardProps["status"],
  confidence: string | null,
): string {
  if (status === "running") return "bg-info animate-pulse";
  if (status === "failed") return "bg-error";
  // done
  if (confidence === "HIGH" || confidence === "MEDIUM") return "bg-success";
  if (confidence === "LOW") return "bg-warning";
  return "bg-muted-foreground";
}

function confidenceTone(
  confidence: string | null,
): "success" | "warning" | "neutral" {
  if (confidence === "HIGH" || confidence === "MEDIUM") return "success";
  if (confidence === "LOW") return "warning";
  return "neutral";
}

function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatCost(usd: number): string {
  return `$${usd.toFixed(4)}`;
}

function formatCacheHit(cacheIn: number, tokensIn: number): string {
  if (tokensIn === 0) return "0%";
  return `${Math.round((cacheIn / tokensIn) * 100)}%`;
}

export function AgentRunCard({ row, status, durationMs, onSelect }: AgentRunCardProps) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "w-full text-left rounded-md border bg-secondary/30 px-3 py-2",
        "hover:bg-secondary/60 transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      )}
    >
      <div className="flex items-center justify-between text-sm">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={cn("inline-block w-2 h-2 rounded-full shrink-0", dotClass(status, row.confidence))}
          />
          <span className="font-medium truncate">{row.agent_role}</span>
          <span className="text-muted-foreground font-mono shrink-0">{row.model}</span>
        </div>
        <div className="flex items-center gap-3 shrink-0 ml-3 text-muted-foreground font-mono">
          <span>{formatDuration(durationMs)}</span>
          <span>{formatCost(row.cost_usd)}</span>
        </div>
      </div>

      <div className="mt-0.5 text-xs text-muted-foreground font-mono">
        in {row.tokens_in.toLocaleString()}
        {"  "}out {row.tokens_out.toLocaleString()}
        {"  "}cache_hit {formatCacheHit(row.cache_input_tokens, row.tokens_in)}
        {"  "}thinking {row.thinking_tokens.toLocaleString()}
      </div>

      <div className="mt-1 flex items-center gap-2 text-xs">
        {row.confidence !== null && (
          <StatusPill tone={confidenceTone(row.confidence)}>
            {row.confidence}
          </StatusPill>
        )}
        {row.citations_count > 0 && (
          <span className="text-muted-foreground font-mono">
            citations {row.citations_count}
          </span>
        )}
      </div>
    </button>
  );
}
