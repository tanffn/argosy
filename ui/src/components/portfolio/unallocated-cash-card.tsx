"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import { api, type UnallocatedCashProposalDTO } from "@/lib/api";

interface Props {
  userId: string;
  overageRatio?: number;
}

/**
 * /portfolio tile: "$X above your cash target -> here's where it could go".
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

  useEffect(() => {
    let cancelled = false;
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
            {proposal.proposals.map((p, i) => (
              <ProposalRow key={i} proposal={p} />
            ))}
          </div>
        )}

        <div className="mt-4 text-xs text-muted-foreground">
          Trigger: current cash &gt; plan-target cash &times;{" "}
          {overageRatio.toFixed(1)}. Self-tuning &mdash; no hard-coded dollar
          threshold. See{" "}
          <Link
            href="/retirement#windfall"
            className="text-info hover:underline"
          >
            /retirement
          </Link>{" "}
          for the cross-month windfall flow ($25K+ deltas).
        </div>
      </CardContent>
    </Card>
  );
}

function ProposalRow({
  proposal,
}: {
  proposal: UnallocatedCashProposalDTO["proposals"][number];
}) {
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
    </div>
  );
}
