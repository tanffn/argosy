"use client";

import { useEffect, useState } from "react";

import { Markdown } from "@/components/markdown";
import { Badge } from "@/components/ui/badge";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { api, type AgentActivityRow } from "@/lib/api";

interface AgentReasoningDrawerProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  userId: string;
  decisionId: string | null;
  agentRole: string | null;
}

export function AgentReasoningDrawer(props: AgentReasoningDrawerProps) {
  const { open, onOpenChange, userId, decisionId, agentRole } = props;
  const [row, setRow] = useState<AgentActivityRow | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !decisionId || !agentRole) {
      // W10 — Option 1: conditional setState so the reset is a no-op
      // on first render (row is already null). The remaining cases
      // (a prior fetch had populated row before the drawer closed)
      // are exactly the "reset stale fetch result on prop change"
      // pattern the React docs allow. The explicit disable below
      // documents why this set-state-in-effect is intentional.
      // eslint-disable-next-line react-hooks/set-state-in-effect -- justified: prop-driven reset of stale fetch results (see comment).
      setRow((prev) => (prev === null ? prev : null));
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .agentActivity(userId, 500, { detail: true, decisionId })
      .then((data) => {
        if (cancelled) return;
        const match = data.rows.find((r) => r.agent_role === agentRole);
        setRow(match ?? null);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, decisionId, agentRole, userId]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full sm:max-w-3xl overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            {agentRole ?? "Agent"}
            {row?.confidence && (
              <Badge variant="outline" className="text-xs">
                {row.confidence}
              </Badge>
            )}
          </SheetTitle>
          <SheetDescription className="font-mono text-xs">
            {decisionId ?? "—"}
            {row?.model ? ` · ${row.model}` : ""}
            {row?.tokens_in
              ? ` · in:${row.tokens_in.toLocaleString()} out:${row.tokens_out.toLocaleString()}`
              : ""}
          </SheetDescription>
        </SheetHeader>

        {loading && (
          <p className="text-sm text-muted-foreground mt-4">Loading…</p>
        )}
        {error && (
          <p className="text-sm text-error font-mono mt-4">{error}</p>
        )}

        {row && !loading && (
          <div className="mt-4">
            {row.response_text ? (
              <Markdown>{row.response_text}</Markdown>
            ) : (
              <p className="text-sm text-muted-foreground">
                Agent emitted no text output.
              </p>
            )}
          </div>
        )}

        {!loading && !row && !error && (open && decisionId && agentRole) && (
          <p className="text-sm text-muted-foreground mt-4">
            No agent_reports row for {agentRole} on {decisionId}.
          </p>
        )}
      </SheetContent>
    </Sheet>
  );
}
