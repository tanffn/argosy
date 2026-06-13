"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type AllocationActionListItem,
  type AllocationActionRequest,
  type UnallocatedCashProposalDTO,
} from "@/lib/api";

interface Props {
  userId: string;
  overageRatio?: number;
}

/** JSON identity for the (snapshot, horizon, asset_class, instrument)
 *  tuple used as source_ref so the same proposal accepted twice dedups
 *  at the DB layer and distinct proposals from the same snapshot don't
 *  collide. Kept terse for index-friendliness.
 */
function buildSourceRef(args: {
  snapshotDate: string | null;
  horizon: string;
  assetClass: string;
  instrument: string;
}): string {
  return JSON.stringify({
    snapshot_date: args.snapshotDate,
    horizon: args.horizon,
    asset_class: args.assetClass,
    instrument: args.instrument,
  });
}

/**
 * /proposals tile: "$X above your cash target -> here's where it could go".
 *
 * The continuous version of the windfall flow ([[unallocated cash]] memory).
 * Routine paycheck residue below the $25K windfall threshold still needs
 * allocation; this tile surfaces a self-tuning trigger based on the user's
 * plan-target cash row (not a hard-coded dollar threshold).
 *
 * Renders nothing when no overage detected -- the tile only appears when
 * Argosy thinks the user has actionable unallocated cash.
 */
export function UnallocatedCashCard({ userId, overageRatio = 1.5 }: Props) {
  const [proposal, setProposal] = useState<UnallocatedCashProposalDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  /** Map of source_ref → most recent decision. Populated on mount + on
   *  each Accept/Defer click so the inline pill renders without a
   *  refetch. */
  const [decisions, setDecisions] = useState<
    Map<string, AllocationActionListItem>
  >(new Map());

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional reset-on-deps-change: show the spinner before each refetch.
    setLoading(true);
    api
      .portfolioUnallocatedCashProposal(userId, overageRatio)
      .then((data) => {
        if (cancelled) return;
        setProposal(data);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId, overageRatio]);

  // Fetch prior allocation_actions for this user + source so each row
  // can render its current decision state. Re-runs when the proposal
  // (and therefore the snapshot_date) changes.
  useEffect(() => {
    if (proposal === null) return;
    let cancelled = false;
    api
      .proposalAllocationActionsList(userId, {
        actionSource: "unallocated_cash",
      })
      .then((resp) => {
        if (cancelled) return;
        const next = new Map<string, AllocationActionListItem>();
        for (const a of resp.actions) {
          if (a.source_ref) next.set(a.source_ref, a);
        }
        setDecisions(next);
      })
      .catch(() => {
        /* swallow — pill just doesn't render */
      });
    return () => {
      cancelled = true;
    };
  }, [userId, proposal]);

  const onDecided = (sourceRef: string, action: AllocationActionListItem) => {
    setDecisions((prev) => {
      const next = new Map(prev);
      next.set(sourceRef, action);
      return next;
    });
  };

  if (loading) return null;
  if (error || proposal === null) return null;

  const excessK = proposal.excess_usd / 1000;

  return (
    <Card className="border-warning/30">
      <CardHeader>
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <CardTitle className="text-base font-mono">
              Unallocated cash &mdash; proposed allocation
            </CardTitle>
            <CardDescription className="mt-1">
              {proposal.headline}
            </CardDescription>
            {proposal.snapshot_date ? (
              <div className="mt-1 text-[11px] text-muted-foreground font-mono">
                Based on snapshot dated {proposal.snapshot_date}
              </div>
            ) : null}
          </div>
          <StatusPill tone="warning" mono>
            ${excessK.toFixed(0)}K excess
          </StatusPill>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-3 gap-2 mb-4 text-xs font-mono text-muted-foreground">
          <div>
            <div className="text-foreground font-semibold text-sm">
              ${proposal.current_cash_k_usd.toFixed(0)}K
            </div>
            <div>current cash</div>
          </div>
          <div>
            <div className="text-foreground font-semibold text-sm">
              ${proposal.target_cash_k_usd.toFixed(0)}K
            </div>
            <div>plan target</div>
          </div>
          <div>
            <div className="text-foreground font-semibold text-sm">
              {proposal.overage_ratio.toFixed(1)}x
            </div>
            <div>overage ratio</div>
          </div>
        </div>

        {proposal.proposals.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No under-target asset class found to absorb the overage. Cash
            sits above plan target but the rest of the portfolio is on or
            above its targets too &mdash; review your plan-target weights.
          </p>
        ) : (
          <div className="space-y-2">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Suggested buys
            </div>
            {proposal.proposals.map((p, i) => {
              const sourceRef = buildSourceRef({
                snapshotDate: proposal.snapshot_date,
                horizon: p.horizon,
                assetClass: p.asset_class,
                instrument: p.instrument,
              });
              return (
                <ProposalRow
                  key={i}
                  proposal={p}
                  userId={userId}
                  snapshotDate={proposal.snapshot_date}
                  sourceRef={sourceRef}
                  prior={decisions.get(sourceRef) ?? null}
                  onDecided={(action) => onDecided(sourceRef, action)}
                />
              );
            })}
          </div>
        )}

        <div className="mt-4 text-xs text-muted-foreground">
          Trigger: current cash &gt; plan-target cash &times;{" "}
          {overageRatio.toFixed(1)}. Self-tuning &mdash; no hard-coded dollar
          threshold. See{" "}
          <Link
            href="/proposals#allocation"
            className="text-info hover:underline"
          >
            /proposals#allocation
          </Link>{" "}
          for the cross-month windfall flow ($25K+ deltas).
        </div>
      </CardContent>
    </Card>
  );
}

interface ProposalRowProps {
  proposal: UnallocatedCashProposalDTO["proposals"][number];
  userId: string;
  snapshotDate: string | null;
  sourceRef: string;
  prior: AllocationActionListItem | null;
  onDecided: (action: AllocationActionListItem) => void;
}

function ProposalRow({
  proposal,
  userId,
  snapshotDate,
  sourceRef,
  prior,
  onDecided,
}: ProposalRowProps) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (status: "accepted" | "deferred") => {
    if (busy) return;
    setBusy(true);
    setErr(null);
    const payload: AllocationActionRequest = {
      user_id: userId,
      action_source: "unallocated_cash",
      // The unallocated-cash detector runs at snapshot-ingest time;
      // we approximate detection time with the snapshot's date. Falls
      // back to "now" if the snapshot is unstamped (shouldn't happen
      // in the live flow).
      source_detected_at: (snapshotDate ? `${snapshotDate}T00:00:00Z` : new Date().toISOString()),
      source_ref: sourceRef,
      horizon: proposal.horizon,
      asset_class: proposal.asset_class,
      instrument: proposal.instrument,
      amount_usd: proposal.amount_usd,
      rationale: proposal.rationale,
      closes_delta_usd: proposal.closes_delta_usd,
      confidence: proposal.confidence,
    };
    try {
      const fn =
        status === "accepted"
          ? api.proposalAllocationAccept
          : api.proposalAllocationDefer;
      const resp = await fn(payload);
      onDecided({
        id: resp.id,
        action_source: "unallocated_cash",
        source_detected_at: payload.source_detected_at,
        source_ref: sourceRef,
        horizon: proposal.horizon,
        asset_class: proposal.asset_class,
        instrument: proposal.instrument,
        amount_usd: proposal.amount_usd,
        decided_status: resp.decided_status,
        decided_at: resp.decided_at,
        due_date: resp.due_date,
        user_note: null,
        proposal_id: null,
      });
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-md border border-border bg-secondary/30 px-3 py-2 font-mono text-xs">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-semibold text-sm">{proposal.instrument}</span>
        <Badge variant="outline" className="text-xs">
          {proposal.asset_class}
        </Badge>
        <span className="text-foreground">
          ${(proposal.amount_usd / 1000).toFixed(1)}K
        </span>
        <span className="text-muted-foreground">
          ({proposal.confidence} confidence)
        </span>
      </div>
      <div className="mt-1 text-muted-foreground whitespace-pre-wrap leading-relaxed">
        {proposal.rationale}
      </div>
      <div className="mt-2 flex items-center gap-2 flex-wrap">
        {prior ? (
          <Badge
            variant={prior.decided_status === "accepted" ? "success" : "secondary"}
            className="text-[11px]"
          >
            {prior.decided_status === "accepted"
              ? `✓ Accepted at ${new Date(prior.decided_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
              : `↻ Deferred${prior.due_date ? ` · due ${prior.due_date}` : ""}`}
          </Badge>
        ) : (
          <>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => submit("accepted")}
              className="h-7 text-[11px]"
            >
              Accept
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => submit("deferred")}
              className="h-7 text-[11px]"
            >
              Defer
            </Button>
          </>
        )}
        {err && <span className="text-rose-400 text-[11px]">{err}</span>}
      </div>
    </div>
  );
}
