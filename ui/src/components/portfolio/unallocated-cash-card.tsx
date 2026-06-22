"use client";

import { useEffect, useState } from "react";

import { StatusPill } from "@/components/ui/status-pill";
import { api, type UnallocatedCashProposalDTO } from "@/lib/api";

interface Props {
  userId: string;
  overageRatio?: number;
}

/**
 * /proposals deploy-cash lead-in: one line — "$X above your cash target is
 * deployable" — sized off the user's plan-target cash row (self-tuning, not a
 * hard-coded threshold). It is the DETECTION line only; the buy list (where it
 * goes) is the DeployCashCard below it, and the source breakdown is the
 * WindfallCard below that — so this stays a single line, not a competing panel.
 *
 * Renders nothing when no overage detected.
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
    <div className="rounded-lg border border-warning/30 bg-warning/5 px-4 py-2.5 flex items-center justify-between gap-3 flex-wrap">
      <p className="text-sm">
        <span className="font-semibold">
          ${excessK.toFixed(0)}K is above your cash target
        </span>{" "}
        <span className="text-muted-foreground">
          (${proposal.current_cash_k_usd.toFixed(0)}K cash vs $
          {proposal.target_cash_k_usd.toFixed(0)}K target) — deployable in the
          buy list below.
        </span>
      </p>
      <StatusPill tone="warning" mono>
        ${excessK.toFixed(0)}K deployable
      </StatusPill>
    </div>
  );
}
