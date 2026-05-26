"use client";

import { useMemo } from "react";
import { Check, X, AlertTriangle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type {
  DraftResponse,
  FMObjectionsResponse,
  HorizonView,
} from "@/lib/api";

import { FMObjectionsCard } from "./fm-objections-card";

interface ExecutiveSummaryCardProps {
  draft: DraftResponse;
  objections: FMObjectionsResponse | null;
  userId: string;
  working?: boolean;
  onAcceptAll?: () => void | Promise<void>;
  onRejectAll?: () => void | Promise<void>;
  onResynthesize?: () => void | Promise<void>;
  resynthesizing?: boolean;
  onDiscussObjection?: (
    objection: { topic: string; detail: string; severity: string },
  ) => void;
  // Called when the user clicks "Start new round with my decisions" and
  // the start-new-round endpoint returns 202. Propagates the audit token
  // and decision_run_id so the parent page can wire the synthesis
  // banner without re-fetching.
  onStartNewRound?: (
    decisionAuditToken: string,
    decisionRunId: number,
  ) => void;
}

function countDeltasByKind(h: HorizonView | null): {
  added: number;
  modified: number;
  removed: number;
  total: number;
} {
  const out = { added: 0, modified: 0, removed: 0, total: 0 };
  if (!h) return out;
  for (const d of h.deltas_from_prior) {
    out[d.change_kind] += 1;
    out.total += 1;
  }
  return out;
}

function horizonStatusBadge(h: HorizonView | null) {
  if (!h) return { label: "—", variant: "outline" as const };
  switch (h.status) {
    case "major_revision":
      return { label: "major", variant: "error" as const };
    case "minor_revision":
      return { label: "minor", variant: "secondary" as const };
    case "no_change":
      return { label: "no change", variant: "success" as const };
  }
}

function truncateAt(s: string, n: number): string {
  if (s.length <= n) return s;
  // Try to break at the next sentence boundary near n.
  const cut = s.slice(0, n);
  const lastDot = cut.lastIndexOf(". ");
  if (lastDot > n * 0.6) return cut.slice(0, lastDot + 1);
  return cut + "…";
}

export function ExecutiveSummaryCard(props: ExecutiveSummaryCardProps) {
  const {
    draft,
    objections,
    userId,
    working,
    onAcceptAll,
    onRejectAll,
    onResynthesize,
    resynthesizing,
    onDiscussObjection,
    onStartNewRound,
  } = props;
  const fmRejected = objections?.approved === false;

  const totals = useMemo(() => {
    const long = countDeltasByKind(draft.horizon_long);
    const med = countDeltasByKind(draft.horizon_medium);
    const sho = countDeltasByKind(draft.horizon_short);
    return {
      total: long.total + med.total + sho.total,
      added: long.added + med.added + sho.added,
      modified: long.modified + med.modified + sho.modified,
      removed: long.removed + med.removed + sho.removed,
    };
  }, [draft]);

  const longBadge = horizonStatusBadge(draft.horizon_long);
  const medBadge = horizonStatusBadge(draft.horizon_medium);
  const shoBadge = horizonStatusBadge(draft.horizon_short);

  return (
    <Card
      className={
        fmRejected
          ? "border-error/40 bg-gradient-to-br from-error/5 to-background"
          : objections
            ? "border-success/40 bg-gradient-to-br from-success/5 to-background"
            : undefined
      }
    >
      <CardHeader>
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <CardTitle className="flex items-center gap-2 text-lg">
              {fmRejected ? (
                <>
                  <AlertTriangle className="h-5 w-5 text-error" />
                  Draft #{draft.plan_version_id} · Fund Manager rejected
                </>
              ) : objections ? (
                <>
                  <Check className="h-5 w-5 text-success" />
                  Draft #{draft.plan_version_id} · Fund Manager approved
                </>
              ) : (
                <>Draft #{draft.plan_version_id} · loading verdict…</>
              )}
            </CardTitle>
            <CardDescription className="font-mono text-xs">
              Drafted {new Date(draft.drafted_at).toLocaleString()}
              {draft.derived_from_id != null
                ? ` · derived from baseline #${draft.derived_from_id}`
                : ""}
              {draft.decision_run_id != null
                ? ` · run #${draft.decision_run_id}`
                : ""}
              {draft.version_label ? ` · ${draft.version_label}` : ""}
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            {onAcceptAll && (
              <Button onClick={onAcceptAll} disabled={working}>
                <Check className="h-4 w-4 mr-1" /> Accept all
              </Button>
            )}
            {onRejectAll && (
              <Button
                onClick={onRejectAll}
                disabled={working}
                variant="outline"
              >
                <X className="h-4 w-4 mr-1" /> Reject + re-synthesize
              </Button>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="rounded-md border border-border/60 p-3">
            <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
              Verdict
            </div>
            <div className="mt-1 text-sm">
              {objections == null && (
                <span className="text-muted-foreground">loading…</span>
              )}
              {objections != null && (
                <>
                  <Badge
                    variant={fmRejected ? "error" : "success"}
                    className="mr-1"
                  >
                    {fmRejected ? "REJECTED" : "APPROVED"}
                  </Badge>
                  {fmRejected && (
                    <span className="text-muted-foreground">
                      {objections.objections.length} objections
                    </span>
                  )}
                </>
              )}
            </div>
          </div>
          <div className="rounded-md border border-border/60 p-3">
            <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
              Deltas
            </div>
            <div className="mt-1 text-sm">
              <span className="font-mono font-semibold">{totals.total}</span>{" "}
              <span className="text-muted-foreground text-xs">
                ({totals.added > 0 && <span className="text-success">+{totals.added}</span>}
                {totals.added > 0 && (totals.modified > 0 || totals.removed > 0) && " "}
                {totals.modified > 0 && <span>~{totals.modified}</span>}
                {totals.modified > 0 && totals.removed > 0 && " "}
                {totals.removed > 0 && <span className="text-error">−{totals.removed}</span>})
              </span>
            </div>
          </div>
          <div className="rounded-md border border-border/60 p-3">
            <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
              Per-horizon status
            </div>
            <div className="mt-1 flex items-center gap-2 text-xs">
              <span>L</span>
              <Badge variant={longBadge.variant} className="text-[10px]">
                {longBadge.label}
              </Badge>
              <span className="ml-1">M</span>
              <Badge variant={medBadge.variant} className="text-[10px]">
                {medBadge.label}
              </Badge>
              <span className="ml-1">S</span>
              <Badge variant={shoBadge.variant} className="text-[10px]">
                {shoBadge.label}
              </Badge>
            </div>
          </div>
        </div>

        {draft.horizon_long?.posture && (
          <div>
            <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1">
              Posture (long horizon)
            </div>
            <p className="text-sm leading-relaxed text-muted-foreground">
              {truncateAt(draft.horizon_long.posture, 320)}
            </p>
          </div>
        )}

        {fmRejected && objections && (
          <FMObjectionsCard
            objections={objections.objections}
            userId={userId}
            planVersionId={draft.plan_version_id}
            onResynthesize={onResynthesize}
            resynthesizing={resynthesizing}
            onDiscussObjection={onDiscussObjection}
            onStartNewRound={onStartNewRound}
          />
        )}
      </CardContent>
    </Card>
  );
}
