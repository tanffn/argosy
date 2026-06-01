"use client";

/**
 * Wave 8 Piece G — HeadlineCard.
 *
 * Sits at the top of the /plan recap layout. Renders the three-line
 * plain-English headline (retirement readiness / next big move / then)
 * plus four at-a-glance tiles (accepted deltas / total portfolio value
 * / insurance gaps / audit). Every field degrades gracefully because
 * the backend service marks all of these as best-effort.
 */

import Link from "next/link";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { RecapSummaryDTO } from "@/lib/api";
import { formatLocalDateTime } from "@/lib/utils";

interface HeadlineCardProps {
  recap: RecapSummaryDTO;
}

function formatUsdK(value: number | null): string {
  if (value == null) return "—";
  // value is in thousands of USD; render as "$2.3M" / "$847k" / "$120k"
  const usd = value * 1000;
  if (usd >= 1_000_000) {
    return `$${(usd / 1_000_000).toFixed(2)}M`;
  }
  if (usd >= 1_000) {
    return `$${Math.round(usd / 1_000)}k`;
  }
  return `$${usd.toFixed(0)}`;
}

export function HeadlineCard({ recap }: HeadlineCardProps) {
  const { headline, accepted_deltas, portfolio_value, insurance_gaps, audit } =
    recap;
  const approvedLabel = audit.approved_at
    ? formatLocalDateTime(audit.approved_at)
    : null;
  return (
    <Card data-slot="headline-card">
      <CardHeader>
        <CardTitle className="text-base">Your plan, in plain English</CardTitle>
        <CardDescription>
          Approved snapshot of the current plan. The headline lines pull
          from the cashflow projection + the soonest-dated actions; the
          tiles below summarize what changed in this round, your total
          portfolio value, insurance coverage, and the audit trail.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <p className="text-lg font-semibold leading-tight">
            {headline.retirement_readiness}
          </p>
          {headline.next_big_move ? (
            <p className="text-sm text-muted-foreground">
              {headline.next_big_move}
            </p>
          ) : null}
          {headline.then ? (
            <p className="text-sm text-muted-foreground">{headline.then}</p>
          ) : null}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {/* Tile 1 — accepted deltas */}
          <div
            className="rounded-md border border-border/60 bg-muted/20 p-3"
            data-slot="tile-accepted-deltas"
          >
            <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
              What changed this round
            </p>
            {accepted_deltas.length === 0 ? (
              <p className="text-sm mt-1 text-muted-foreground">
                No accepted deltas yet.
              </p>
            ) : (
              <ul className="text-sm mt-1 flex flex-col gap-1">
                {accepted_deltas.slice(0, 4).map((d, i) => (
                  <li key={`${d.horizon}-${d.item_kind}-${i}`}>
                    <span className="text-xs uppercase font-mono text-muted-foreground mr-1.5">
                      [{d.horizon}]
                    </span>
                    {d.summary}
                  </li>
                ))}
                {accepted_deltas.length > 4 ? (
                  <li className="text-xs text-muted-foreground">
                    +{accepted_deltas.length - 4} more
                  </li>
                ) : null}
              </ul>
            )}
          </div>

          {/* Tile 2 — portfolio value anchor */}
          <div
            className="rounded-md border border-border/60 bg-muted/20 p-3"
            data-slot="tile-portfolio-value"
          >
            <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Total portfolio
            </p>
            <p className="text-2xl font-semibold mt-1">
              {formatUsdK(portfolio_value.total_usd_value_k)}
            </p>
            <p className="text-xs text-muted-foreground">
              {portfolio_value.snapshot_date
                ? `as of ${portfolio_value.snapshot_date}`
                : "no snapshot on record"}
            </p>
          </div>

          {/* Tile 3 — insurance gaps */}
          <div
            className="rounded-md border border-border/60 bg-muted/20 p-3"
            data-slot="tile-insurance-gaps"
          >
            <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Insurance gaps
            </p>
            <p
              className={
                insurance_gaps.has_data
                  ? "text-sm mt-1"
                  : "text-sm mt-1 text-muted-foreground"
              }
            >
              {insurance_gaps.one_line}
            </p>
          </div>

          {/* Tile 4 — audit line */}
          <div
            className="rounded-md border border-border/60 bg-muted/20 p-3"
            data-slot="tile-audit"
          >
            <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
              Audit trail
            </p>
            <p className="text-sm mt-1">
              plan_version #{audit.plan_version_id}
              {audit.decision_run_id != null ? (
                <>
                  {" "}
                  · run #{audit.decision_run_id}
                </>
              ) : null}
            </p>
            {approvedLabel ? (
              <p className="text-xs text-muted-foreground">
                approved {approvedLabel}
              </p>
            ) : null}
            {audit.synthesis_trail_link ? (
              <Link
                href={audit.synthesis_trail_link}
                className="text-xs text-primary hover:underline"
              >
                View synthesis trail →
              </Link>
            ) : null}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
