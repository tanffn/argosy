"use client";

import { useState } from "react";
import { Check, History, MessageSquareWarning, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, type DeltaItem } from "@/lib/api";

interface DeltaCardProps {
  delta: DeltaItem;
  userId: string;
  disabled?: boolean;
  onAccept?: (delta: DeltaItem) => void | Promise<void>;
  onReject?: (delta: DeltaItem) => void | Promise<void>;
  onPushBack?: (delta: DeltaItem) => void | Promise<void>;
  onSourceClick?: (agentLabel: string) => void;
}

interface HistoryEntry {
  plan_version_id: number;
  version_label: string | null;
  role: string;
  drafted_at: string;
  horizon: string;
  label: string;
  value: number | string | null;
  unit: string | null;
  rationale: string;
  accepted: boolean;
}

function changeKindBadge(kind: DeltaItem["change_kind"]) {
  switch (kind) {
    case "added":
      return { variant: "success" as const, label: "ADD" };
    case "modified":
      return { variant: "secondary" as const, label: "CHANGE" };
    case "removed":
      return { variant: "error" as const, label: "REMOVE" };
  }
}

function itemKindLabel(kind: DeltaItem["item_kind"]): string {
  return kind.replace("_", " ").toUpperCase();
}

// Format a proposed/prior payload into the "<value> <unit>" headline used at
// the top of each card. Strips noisy keys (label/rationale/source_section)
// since the card already shows label + rationale separately.
function formatTargetValue(p: Record<string, unknown> | null): string | null {
  if (!p) return null;
  const value = p.value;
  const unit = (p.unit as string | undefined) ?? "";
  if (typeof value === "number") {
    if (unit.includes("pct")) return `${value}%`;
    if (unit.includes("usd") || unit === "$") return `$${value.toLocaleString()}`;
    if (unit) return `${value.toLocaleString()} ${unit}`;
    return String(value);
  }
  if (typeof value === "string" && value) {
    return unit ? `${value} ${unit}` : value;
  }
  // Action shape: { when, ticker, side, qty }
  const parts: string[] = [];
  if (typeof p.side === "string") parts.push(p.side.toUpperCase());
  if (typeof p.qty === "number" || typeof p.qty === "string") parts.push(String(p.qty));
  if (typeof p.ticker === "string") parts.push(p.ticker);
  if (typeof p.when === "string") parts.push(`(${p.when})`);
  return parts.length > 0 ? parts.join(" ") : null;
}

function proposedLabel(p: Record<string, unknown> | null): string | null {
  if (!p || typeof p !== "object") return null;
  const lbl = p.label;
  return typeof lbl === "string" && lbl ? lbl : null;
}

export function DeltaCard(props: DeltaCardProps) {
  const {
    delta,
    userId,
    disabled,
    onAccept,
    onReject,
    onPushBack,
    onSourceClick,
  } = props;
  const [rejectedLocally, setRejectedLocally] = useState(false);
  const [history, setHistory] = useState<
    HistoryEntry[] | "loading" | "error" | null
  >(null);

  const loadHistory = async () => {
    setHistory("loading");
    try {
      const r = await api.planItemHistory(userId, delta.item_id);
      setHistory(r.entries);
    } catch {
      setHistory("error");
    }
  };

  const toggleHistory = () => {
    if (history === null || history === "error") {
      void loadHistory();
    } else {
      setHistory(null);
    }
  };
  const badge = changeKindBadge(delta.change_kind);

  const propValue = formatTargetValue(delta.proposed);
  const propLabel = proposedLabel(delta.proposed);
  const priorValue = formatTargetValue(delta.prior);
  const labels = delta.provenance_agent_labels ?? [];

  // Backend stores REJECTED / PUSHBACK in user_edit_note with a prefix. Parse
  // those so the card surfaces persistent state after a refresh, not just the
  // local-React click-state.
  const editNote = delta.user_edit_note ?? "";
  const persistedRejected = editNote.startsWith("REJECTED");
  const pushbackLines = editNote
    .split("\n")
    .filter((l) => l.startsWith("PUSHBACK:"))
    .map((l) => l.slice("PUSHBACK:".length).trim());

  const isAccepted = delta.accepted;
  const isRejected = rejectedLocally || persistedRejected;

  return (
    <article
      className={`rounded-md border p-4 transition-colors ${
        isAccepted
          ? "border-success/40 bg-success/5"
          : isRejected
            ? "border-error/40 bg-error/5 opacity-70"
            : "border-border bg-background"
      }`}
    >
      <header className="flex items-start justify-between gap-3 mb-2 flex-wrap">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={badge.variant}>{badge.label}</Badge>
          <span className="text-[10px] font-mono text-muted-foreground uppercase tracking-wide">
            {itemKindLabel(delta.item_kind)}
          </span>
          <span className="text-[10px] font-mono text-muted-foreground">
            {delta.item_id}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {isAccepted && (
            <Badge variant="outline" className="text-success border-success/40">
              <Check className="h-3 w-3 mr-1" /> accepted
            </Badge>
          )}
          {isRejected && (
            <Badge variant="outline" className="text-error border-error/40">
              <X className="h-3 w-3 mr-1" /> rejected
            </Badge>
          )}
        </div>
      </header>

      {/* Headline: the agent's suggestion in one sentence. */}
      <p className="text-sm font-medium leading-snug">{delta.summary}</p>

      {/* The structured proposed value, rendered explicitly so the
          "currently it sits on … / I suggest …" comparison is obvious. */}
      {(propValue || propLabel || priorValue) && (
        <div className="mt-3 rounded-md bg-muted/30 px-3 py-2">
          {propLabel && (
            <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
              {propLabel}
            </div>
          )}
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 mt-0.5 text-sm">
            {propValue && (
              <span>
                <span className="text-[10px] font-mono uppercase mr-1 text-muted-foreground">
                  suggested
                </span>
                <span className="font-mono font-semibold">{propValue}</span>
              </span>
            )}
            {priorValue && (
              <span>
                <span className="text-[10px] font-mono uppercase mr-1 text-muted-foreground">
                  before
                </span>
                <span className="font-mono">{priorValue}</span>
              </span>
            )}
          </div>
        </div>
      )}

      {/* Rationale always visible (was collapsible) so the user doesn't
          have to expand 10 cards to read the reasoning. The synthesizer
          writes 1-2 sentence rationales; they're short. */}
      {delta.rationale && (
        <div className="mt-3">
          <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1">
            Reasoning
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            {delta.rationale}
          </p>
        </div>
      )}

      {labels.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
            sources
          </span>
          {labels.map((label) => (
            <button
              key={label}
              type="button"
              onClick={() => onSourceClick?.(label)}
              className="rounded-full bg-accent/30 hover:bg-accent/60 transition-colors px-2 py-0.5 text-[10px] font-mono"
              title="Open the agent's full reasoning"
            >
              {label}
            </button>
          ))}
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={toggleHistory}
          title="Show how this item has evolved across plan iterations"
        >
          <History className="h-3 w-3 mr-1" />
          {history === "loading"
            ? "Loading…"
            : Array.isArray(history)
              ? "Hide history"
              : "History"}
        </Button>
      </div>

      {Array.isArray(history) && history.length > 0 && (
        <div className="mt-3 rounded-md border border-border/40 bg-muted/20 p-3">
          <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-2">
            Item lineage ({history.length} versions)
          </div>
          <ul className="text-xs space-y-2">
            {history.map((h, i) => {
              const valueStr =
                h.value !== null
                  ? `${h.value}${h.unit ? " " + h.unit : ""}`
                  : "—";
              return (
                <li
                  key={`${h.plan_version_id}-${i}`}
                  className="flex items-baseline gap-2 border-l-2 border-border/40 pl-2"
                >
                  <span className="font-mono text-muted-foreground text-[10px] min-w-[64px]">
                    plan #{h.plan_version_id}
                  </span>
                  <Badge variant="outline" className="text-[10px]">
                    {h.role}
                  </Badge>
                  <span className="font-mono">{valueStr}</span>
                  {h.label && (
                    <span className="text-muted-foreground truncate">
                      — {h.label}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {Array.isArray(history) && history.length === 0 && (
        <p className="mt-3 text-xs text-muted-foreground">
          No prior versions for this item — first appearance.
        </p>
      )}
      {history === "error" && (
        <p className="mt-3 text-xs text-error">
          Couldn&apos;t load history; the endpoint may not be available yet.
        </p>
      )}

      {pushbackLines.length > 0 && (
        <div className="mt-3 rounded-md border border-warning/40 bg-warning/5 px-3 py-2">
          <div className="text-[10px] font-mono uppercase tracking-wide text-warning mb-1">
            Your pushback ({pushbackLines.length})
          </div>
          <ul className="text-xs space-y-1">
            {pushbackLines.map((line, i) => (
              <li key={i} className="text-muted-foreground">
                · {line}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!isAccepted && !isRejected && (
        <div className="mt-3 flex flex-wrap justify-end gap-2">
          {onPushBack && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onPushBack(delta)}
              disabled={disabled}
              title="Tell the fleet why this isn't right; they re-evaluate with your pushback"
            >
              <MessageSquareWarning className="h-3.5 w-3.5 mr-1" /> Push back
            </Button>
          )}
          {onReject && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setRejectedLocally(true);
                onReject(delta);
              }}
              disabled={disabled}
            >
              <X className="h-3.5 w-3.5 mr-1" /> Reject
            </Button>
          )}
          {onAccept && (
            <Button
              size="sm"
              onClick={() => onAccept(delta)}
              disabled={disabled}
            >
              <Check className="h-3.5 w-3.5 mr-1" /> Accept
            </Button>
          )}
        </div>
      )}
    </article>
  );
}
