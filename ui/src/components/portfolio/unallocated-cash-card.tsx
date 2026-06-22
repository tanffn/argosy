"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

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
 * /proposals detection banner: "$X above your cash target → it's deployable".
 *
 * The continuous version of the windfall flow ([[unallocated cash]] memory).
 * Routine paycheck residue below the $25K windfall threshold still needs
 * allocation; this banner surfaces a self-tuning trigger based on the user's
 * plan-target cash row (not a hard-coded dollar threshold).
 *
 * This is the DETECTION surface only — it answers "how much is deployable"
 * (current cash vs plan target vs overage ratio) and feeds the deploy amount.
 * The actionable buy list lives in the DeployCashCard below it; this banner no
 * longer renders a competing list of suggested buys.
 *
 * Renders nothing when no overage detected -- the banner only appears when
 * Argosy thinks the user has actionable unallocated cash.
 */
export function UnallocatedCashCard({ userId, overageRatio = 1.5 }: Props) {
  const [proposal, setProposal] = useState<UnallocatedCashProposalDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

        <div className="mt-4 text-xs text-muted-foreground">
          Trigger: current cash &gt; plan-target cash &times;{" "}
          {overageRatio.toFixed(1)}. Self-tuning &mdash; no hard-coded dollar
          threshold. The deployable amount feeds the buy list below; see{" "}
          <Link
            href="/proposals#deploy-cash"
            className="text-info hover:underline"
          >
            /proposals#deploy-cash
          </Link>{" "}
          to choose where it goes, or{" "}
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
