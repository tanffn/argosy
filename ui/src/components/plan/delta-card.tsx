"use client";

import { useState } from "react";
import { Check, ChevronDown, ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { DeltaItem } from "@/lib/api";

interface DeltaCardProps {
  delta: DeltaItem;
  disabled?: boolean;
  onAccept?: (delta: DeltaItem) => void | Promise<void>;
  onSourceClick?: (agentLabel: string) => void;
}

function changeKindBadge(kind: DeltaItem["change_kind"]) {
  switch (kind) {
    case "added":
      return { variant: "success" as const, label: "ADDED" };
    case "modified":
      return { variant: "secondary" as const, label: "MODIFIED" };
    case "removed":
      return { variant: "error" as const, label: "REMOVED" };
  }
}

function itemKindLabel(kind: DeltaItem["item_kind"]): string {
  return kind.replace("_", " ").toUpperCase();
}

function summarizeProposed(p: Record<string, unknown> | null): string | null {
  if (!p) return null;
  // Common fields on synth targets/actions: value/unit, label, when, ticker.
  const parts: string[] = [];
  if (typeof p.value === "number" || typeof p.value === "string") {
    parts.push(String(p.value));
  }
  if (typeof p.unit === "string" && p.unit) {
    parts.push(p.unit);
  }
  if (typeof p.when === "string" && p.when) {
    parts.push(`(when: ${p.when})`);
  }
  return parts.join(" ") || null;
}

export function DeltaCard(props: DeltaCardProps) {
  const { delta, disabled, onAccept, onSourceClick } = props;
  const [expanded, setExpanded] = useState(false);
  const badge = changeKindBadge(delta.change_kind);
  const proposedSummary = summarizeProposed(delta.proposed);
  const priorSummary = summarizeProposed(delta.prior);
  const labels = delta.provenance_agent_labels ?? [];

  return (
    <article className="rounded-md border border-border bg-background p-4">
      <header className="flex items-start justify-between gap-3 mb-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={badge.variant}>{badge.label}</Badge>
          <span className="text-[10px] font-mono text-muted-foreground uppercase tracking-wide">
            {itemKindLabel(delta.item_kind)}
          </span>
          <span className="text-[10px] font-mono text-muted-foreground">
            {delta.item_id}
          </span>
        </div>
        {delta.accepted && (
          <Badge variant="outline" className="text-success border-success/40">
            <Check className="h-3 w-3 mr-1" /> accepted
          </Badge>
        )}
      </header>

      <p className="text-sm font-medium leading-snug">{delta.summary}</p>

      {(proposedSummary || priorSummary) && (
        <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
          {priorSummary && (
            <>
              <dt className="text-muted-foreground">before</dt>
              <dd className="font-mono">{priorSummary}</dd>
            </>
          )}
          {proposedSummary && (
            <>
              <dt className="text-muted-foreground">after</dt>
              <dd className="font-mono">{proposedSummary}</dd>
            </>
          )}
        </dl>
      )}

      {delta.rationale && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            aria-expanded={expanded}
          >
            {expanded ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            Rationale
          </button>
          {expanded && (
            <p className="mt-1 text-xs text-muted-foreground leading-relaxed pl-4">
              {delta.rationale}
            </p>
          )}
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
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {onAccept && !delta.accepted && (
        <div className="mt-3 flex justify-end">
          <Button
            size="sm"
            variant="outline"
            onClick={() => onAccept(delta)}
            disabled={disabled}
          >
            Accept this delta
          </Button>
        </div>
      )}
    </article>
  );
}
