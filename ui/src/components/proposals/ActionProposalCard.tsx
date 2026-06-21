"use client";

/**
 * ActionProposalCard — Spec E commit #6 / spec §6.1.
 *
 * Renders one row of the unified action-proposal queue:
 *
 *   * Severity dot (red / amber / blue).
 *   * Kind badge.
 *   * Summary (one-line LLM-generated).
 *   * Rationale markdown — collapsed by default; expand on click.
 *   * Structured suggested_payload preview (readonly key: value rows).
 *   * Four action buttons: Accept / Defer / Reject / Customize.
 *
 * The card is presentational only — all four action handlers are
 * passed in by the page so the page owns the modals + the API
 * round-trips. Customize is a special case: the page opens the
 * CustomizeModal which submits an edited payload to the Accept
 * endpoint (Customize === Accept with edits per spec §6.1).
 */

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Markdown } from "@/components/markdown";
import { cn } from "@/lib/utils";
import type { ActionProposalDTO } from "@/lib/api";

// Map an ActionProposalKind to a human-readable badge label. Keep in
// sync with argosy/agents/action_proposer.py:ActionProposalKind. The
// fallback (Title-Case'd kind) means a new backend kind renders
// sanely without a UI redeploy.
const KIND_LABELS: Record<string, string> = {
  allocate: "Allocate",
  repatriate_currency: "Repatriate currency",
  rebalance: "Rebalance",
  replan_full: "Replan plan",
  add_life_event_phase: "Add life-event phase",
  update_plan_assumption: "Update plan assumption",
  set_watchlist: "Set watchlist",
  note_only: "Note",
};

function kindLabel(kind: string): string {
  return (
    KIND_LABELS[kind] ??
    kind
      .split("_")
      .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
      .join(" ")
  );
}

// Severity dot — Tailwind class per severity. Critical = red, warning
// = amber, info = blue. The dot mirrors the existing project-wide
// severity convention (e.g. monitor flags). Kept as a small <span>
// rather than the Badge component so the kind badge is visually
// distinct from the severity indicator.
const SEVERITY_DOT_CLASS: Record<string, string> = {
  critical: "bg-red-500",
  warning: "bg-amber-500",
  info: "bg-blue-500",
};

interface ActionProposalCardProps {
  proposal: ActionProposalDTO;
  busy: boolean;
  onAccept: () => void;
  onDefer: () => void;
  onReject: () => void;
  onCustomize: () => void;
}

export function ActionProposalCard({
  proposal,
  busy,
  onAccept,
  onDefer,
  onReject,
  onCustomize,
}: ActionProposalCardProps) {
  const [rationaleOpen, setRationaleOpen] = useState(false);

  return (
    <Card id={`action-proposal-${proposal.id}`}>
      <CardHeader>
        <div className="flex items-center gap-3 justify-between flex-wrap">
          <div className="flex items-center gap-3 flex-wrap">
            <span
              aria-label={`severity ${proposal.severity}`}
              className={cn(
                "inline-block w-2.5 h-2.5 rounded-full",
                SEVERITY_DOT_CLASS[proposal.severity] ?? "bg-muted",
              )}
            />
            <Badge variant="secondary">{kindLabel(proposal.kind)}</Badge>
            <CardTitle className="text-base">{proposal.summary}</CardTitle>
          </div>
          <div className="text-xs font-mono text-muted-foreground">
            #{proposal.id}
          </div>
        </div>
        <CardDescription className="text-xs font-mono">
          {proposal.surfaced_at
            ? `proposed ${proposal.surfaced_at.slice(0, 10)}`
            : null}
          {proposal.surfaced_at && proposal.expires_at ? " · " : null}
          {proposal.expires_at
            ? `expires ${proposal.expires_at.slice(0, 10)}`
            : null}
        </CardDescription>
      </CardHeader>

      <CardContent className="flex flex-col gap-3">
        {/* Suggested-payload preview — readonly key: value rows. The
            Customize modal renders the same shape in editable mode. */}
        {Object.keys(proposal.suggested_payload).length > 0 && (
          <div className="rounded-md border border-border/40 bg-muted/20 p-3">
            <h4 className="text-xs font-semibold mb-2 text-muted-foreground">
              Proposed change — system-generated, review before acting
            </h4>
            <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs font-mono">
              {Object.entries(proposal.suggested_payload).map(([k, v]) => (
                <PayloadRow key={k} field={k} value={v} />
              ))}
            </dl>
          </div>
        )}

        {/* Rationale toggle. Collapsed by default per spec §6.1. */}
        {proposal.rationale_md && (
          <div>
            <button
              type="button"
              onClick={() => setRationaleOpen((p) => !p)}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              {rationaleOpen ? "▼ Hide rationale" : "▶ Show rationale"}
            </button>
            {rationaleOpen && (
              <div className="mt-2 prose prose-sm max-w-none text-sm">
                <Markdown>{proposal.rationale_md}</Markdown>
              </div>
            )}
          </div>
        )}

        {/* Action buttons. note_only kind shows only Defer / Reject
            (no Accept / Customize) per spec §1.3. */}
        <div className="flex items-center gap-2 mt-1 flex-wrap">
          {proposal.kind !== "note_only" && (
            <Button
              size="sm"
              onClick={onAccept}
              disabled={busy}
            >
              Accept
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            onClick={onDefer}
            disabled={busy}
          >
            Defer
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={onReject}
            disabled={busy}
          >
            Reject
          </Button>
          {proposal.kind !== "note_only" &&
            Object.keys(proposal.suggested_payload).length > 0 && (
              <Button
                size="sm"
                variant="outline"
                onClick={onCustomize}
                disabled={busy}
              >
                Customize
              </Button>
            )}
        </div>
      </CardContent>
    </Card>
  );
}

// One key:value row in the payload preview. Numbers / strings render
// inline; arrays / nested objects fall back to compact JSON so the
// preview stays one-line even for complex payloads (rebalance).
function PayloadRow({ field, value }: { field: string; value: unknown }) {
  const valueText =
    typeof value === "string" || typeof value === "number" ||
    typeof value === "boolean"
      ? String(value)
      : value === null
        ? "null"
        : JSON.stringify(value);
  return (
    <>
      <dt className="text-muted-foreground">{field}</dt>
      <dd className="break-all">{valueText}</dd>
    </>
  );
}
