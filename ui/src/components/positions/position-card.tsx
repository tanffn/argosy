"use client";

import type { PositionThesisDTO } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { friendlySourceLabel } from "@/lib/plain-english-labels";
import { cn } from "@/lib/utils";

/**
 * One-per-position thesis card (T4.1).
 *
 * Color-codes the card border + verdict pill so the user can scan a
 * grid of holdings and see at a glance which ones the fleet wants to
 * trim, sell, hold, buy, or add. ``ADD`` cards (replacement candidates
 * not currently held) get a dashed border to visually separate them
 * from current holdings.
 */

interface PositionCardProps {
  thesis: PositionThesisDTO;
}

function verdictBorderClass(verdict: PositionThesisDTO["verdict"]): string {
  switch (verdict) {
    case "SELL":
      return "border-error/40 hover:border-error/60";
    case "TRIM":
      return "border-warning/40 hover:border-warning/60";
    case "BUY":
      return "border-success/40 hover:border-success/60";
    case "ADD":
      return "border-info/50 border-dashed hover:border-info/70";
    case "HOLD":
    default:
      return "border-border hover:border-foreground/25";
  }
}

function verdictBadgeVariant(
  verdict: PositionThesisDTO["verdict"],
):
  | "default"
  | "secondary"
  | "destructive"
  | "outline"
  | "success"
  | "warning"
  | "error"
  | "info" {
  switch (verdict) {
    case "SELL":
      return "error";
    case "TRIM":
      return "warning";
    case "BUY":
      return "success";
    case "ADD":
      return "info";
    case "HOLD":
    default:
      return "outline";
  }
}

function convictionBadgeVariant(
  conviction: PositionThesisDTO["conviction"],
): "success" | "secondary" | "outline" {
  switch (conviction) {
    case "HIGH":
      return "success";
    case "MEDIUM":
      return "secondary";
    case "LOW":
    default:
      return "outline";
  }
}

function formatUsd(usd: number | null): string {
  if (usd === null) return "—";
  if (usd >= 1_000_000) return `$${(usd / 1_000_000).toFixed(2)}M`;
  if (usd >= 1_000) return `$${(usd / 1_000).toFixed(1)}K`;
  return `$${usd.toFixed(0)}`;
}

function formatShares(shares: number | null): string {
  if (shares === null) return "—";
  if (Math.abs(shares - Math.round(shares)) < 0.001) {
    return Math.round(shares).toLocaleString();
  }
  return shares.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function PositionCard({ thesis }: PositionCardProps) {
  const isAdd = thesis.verdict === "ADD";
  return (
    <Card className={cn("flex flex-col gap-3", verdictBorderClass(thesis.verdict))}>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="font-mono text-lg">{thesis.ticker}</CardTitle>
          <div className="flex items-center gap-1.5">
            <Badge variant={verdictBadgeVariant(thesis.verdict)}>
              {thesis.verdict}
            </Badge>
            <Badge variant={convictionBadgeVariant(thesis.conviction)}>
              {thesis.conviction}
            </Badge>
          </div>
        </div>
        <CardDescription className="font-mono text-xs">
          {isAdd ? (
            <>Not currently held · replacement candidate</>
          ) : (
            <>
              {formatShares(thesis.current_shares)} sh ·{" "}
              {formatUsd(thesis.current_usd_value)}
              {thesis.current_weight_pct !== null && (
                <> · {thesis.current_weight_pct.toFixed(1)}% of portfolio</>
              )}
            </>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {(thesis.target_weight_pct !== null ||
          thesis.target_shares !== null) && (
          <div className="text-xs font-mono text-muted-foreground">
            Target:{" "}
            {thesis.target_weight_pct !== null && (
              <span>{thesis.target_weight_pct.toFixed(1)}% of portfolio</span>
            )}
            {thesis.target_weight_pct !== null &&
              thesis.target_shares !== null && <span> · </span>}
            {thesis.target_shares !== null && (
              <span>{thesis.target_shares.toLocaleString()} sh ceiling</span>
            )}
          </div>
        )}
        {thesis.reasoning_md && (
          <p className="text-sm whitespace-pre-wrap leading-relaxed">
            {thesis.reasoning_md}
          </p>
        )}
        {thesis.cited_sources.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {thesis.cited_sources.slice(0, 6).map((src) => (
              <Badge
                key={src}
                variant="outline"
                className="text-xs"
                title={src}
              >
                {friendlySourceLabel(src)}
              </Badge>
            ))}
            {thesis.cited_sources.length > 6 && (
              <Badge variant="outline" className="text-xs">
                +{thesis.cited_sources.length - 6} more
              </Badge>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
