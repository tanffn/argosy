"use client";

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
import { api, type RebalanceReviewDTO, type RebalanceLeg } from "@/lib/api";

function fmtUsd(n: number): string {
  return `$${Math.round(n).toLocaleString()}`;
}

function fmtPct(n: number | null): string {
  return n === null || n === undefined ? "—" : `${n}%`;
}

function actionVariant(
  action: string,
): "default" | "secondary" | "destructive" | "outline" | "success" | "error" {
  if (action === "SELL") return "error";
  if (action === "TRIM") return "secondary";
  if (action === "BUY") return "success";
  return "outline";
}

function LegsTable({ legs }: { legs: RebalanceLeg[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr className="border-b border-border/40">
            <th className="text-left py-1 pr-3">Action</th>
            <th className="text-left py-1 pr-3">Ticker</th>
            <th className="text-left py-1 pr-3">Class</th>
            <th className="text-right py-1 pr-3">From → To</th>
            <th className="text-right py-1 pr-3">Amount</th>
            <th className="text-left py-1 pr-3">Conviction</th>
            <th className="text-left py-1">Why / gate</th>
          </tr>
        </thead>
        <tbody>
          {legs.map((l, i) => (
            <tr key={`${l.ticker}-${l.action}-${i}`} className="border-b border-border/20 align-top">
              <td className="py-1.5 pr-3">
                <Badge variant={actionVariant(l.action)}>{l.action}</Badge>
              </td>
              <td className="py-1.5 pr-3 font-mono font-medium">{l.ticker}</td>
              <td className="py-1.5 pr-3">{l.asset_class}</td>
              <td className="py-1.5 pr-3 text-right font-mono whitespace-nowrap">
                {fmtPct(l.from_pct)} → {fmtPct(l.to_pct)}
              </td>
              <td className="py-1.5 pr-3 text-right font-mono whitespace-nowrap">
                {fmtUsd(l.amount_usd)}
              </td>
              <td className="py-1.5 pr-3">{l.thesis_conviction ?? "n/a"}</td>
              <td className="py-1.5">
                <code className="font-mono text-[11px]">{l.gate_reason}</code>
                {l.cited_flags.length > 0 && (
                  <span className="text-muted-foreground">
                    {" "}
                    · {l.cited_flags.join(", ")}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReviewBody({ review }: { review: RebalanceReviewDTO }) {
  if (review.status === "cannot_review") {
    return (
      <div className="rounded border border-border/60 bg-muted/20 p-3 text-sm">
        <p className="font-medium">Cannot compose a review right now.</p>
        <p className="text-muted-foreground mt-1">{review.summary}</p>
        {review.cannot_review_reason && (
          <p className="text-xs text-muted-foreground mt-1 font-mono">
            reason: {review.cannot_review_reason}
          </p>
        )}
      </div>
    );
  }

  const netDelta = review.net_cash_delta_usd;
  const netLabel =
    netDelta > 0
      ? "net buy — cash needed"
      : netDelta < 0
      ? "net sell — cash freed"
      : "cash-neutral";

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm">{review.summary}</p>

      {review.legs.length > 0 ? (
        <LegsTable legs={review.legs} />
      ) : (
        <p className="text-sm text-muted-foreground">
          No thesis-gated rebalance legs were warranted (every over-target
          position is a high-conviction intact holding under non-critical drift,
          or no class is over-target).
        </p>
      )}

      <div className="flex items-center gap-2 text-sm">
        <span className="text-muted-foreground">Net cash delta:</span>
        <span className="font-mono font-semibold">
          {netDelta >= 0 ? "+" : ""}
          {fmtUsd(netDelta)}
        </span>
        <span className="text-xs text-muted-foreground">({netLabel})</span>
      </div>

      {review.dropped_buy_candidates.length > 0 && (
        <div className="rounded border border-border/60 bg-muted/20 p-3 text-xs">
          <p className="font-semibold mb-1">
            Dropped buy candidates (estate gate)
          </p>
          <ul className="space-y-1 text-muted-foreground">
            {review.dropped_buy_candidates.map((d, i) => (
              <li key={`${d.ticker}-${i}`}>
                <span className="font-mono text-foreground">{d.ticker}</span>{" "}
                ({d.asset_class}): {d.reason}
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="text-xs text-muted-foreground border-l-2 border-amber-500/60 pl-3">
        Every trim/sell leg is a TAXABLE EVENT — it realizes capital gains
        (Israeli CGT, and a §102 / US-sourced component for RSU lots), so net
        proceeds will be below the gross amount. Confirm the lot-level tax before
        acting; this review does not compute the exact liability. This is a
        PROPOSAL only — nothing executes.
      </p>
    </div>
  );
}

export function RebalanceReviewCard({
  userId,
  onReviewed,
}: {
  userId: string;
  onReviewed?: () => void;
}) {
  const [review, setReview] = useState<RebalanceReviewDTO | null>(null);
  const [proposalWritten, setProposalWritten] = useState<boolean>(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runReview = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.portfolioRebalanceReview(userId);
      setReview(r.review);
      setProposalWritten(r.proposal_written);
      if (r.proposal_written) onReviewed?.();
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <CardTitle className="text-base">Holistic portfolio review</CardTitle>
            <CardDescription className="max-w-3xl mt-1">
              Holistic, plan-driven, news-supported review — trims over-target
              sleeves to fund under-target ones, thesis-gated; proposal-only,
              never executes.
            </CardDescription>
          </div>
          <Button size="sm" onClick={runReview} disabled={loading}>
            {loading ? "Reviewing…" : "Run portfolio review"}
          </Button>
        </div>
      </CardHeader>
      {(error || review) && (
        <CardContent className="flex flex-col gap-3">
          {error && <p className="text-sm text-error font-mono">{error}</p>}
          {review && <ReviewBody review={review} />}
          {review && review.status === "ok" && (
            <p className="text-xs text-muted-foreground">
              {proposalWritten
                ? 'This review was also saved as a proposal — see "Action proposals" below.'
                : "No proposal row was written (no actionable legs)."}
            </p>
          )}
        </CardContent>
      )}
    </Card>
  );
}
