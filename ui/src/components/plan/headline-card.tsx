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
import type { HeadlineDerivationDTO, RecapSummaryDTO } from "@/lib/api";
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
          {recap.derivation ? (
            <p className="text-xs text-muted-foreground">
              based on μ={(recap.derivation.mu_nominal_annual * 100).toFixed(0)}%,
              σ={(recap.derivation.sigma_annual * 100).toFixed(1)}%,
              tax={(recap.derivation.tax_rate * 100).toFixed(0)}%,
              target age {recap.derivation.retirement_target_age.toFixed(0)}.{" "}
              {recap.derivation.sourced_from}
            </p>
          ) : null}
          {headline.next_big_move ? (
            <p className="text-sm text-muted-foreground">
              {headline.next_big_move}
            </p>
          ) : null}
          {headline.then ? (
            <p className="text-sm text-muted-foreground">{headline.then}</p>
          ) : null}
        </div>

        {recap.derivation && recap.derivation.sensitivity_by_mu.length > 0 ? (
          <SensitivityStrip derivation={recap.derivation} />
        ) : null}

        {recap.derivation?.readiness_by_policy &&
        recap.derivation.readiness_by_policy.length > 0 ? (
          <ReadinessByPolicyStrip derivation={recap.derivation} />
        ) : null}

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

/**
 * Wave 8 v2 polish — μ sensitivity strip.
 *
 * The cashflow projection is dominated by the expected-return
 * assumption. A 2-percentage-point change in μ can move the
 * retirement age by 5-15 years, so the user needs to SEE the
 * fragility, not just trust the base-case number.
 */
/**
 * Wave 8 v2.3 — per-policy readiness strip.
 *
 * Shows the three readings side-by-side so the user can compare:
 *   - returns_only: portfolio's real return + annuity ≥ expenses
 *     (capital preservation — never touches principal)
 *   - swr_3_5: Bengen-style 3.5% Safe Withdrawal Rate
 *     (matches the user's plan-stated framework)
 *   - swr_4_0: more aggressive 4% SWR
 */
function ReadinessByPolicyStrip({
  derivation,
}: {
  derivation: HeadlineDerivationDTO;
}) {
  // The backend now emits the three reconciled age ANCHORS (earliest-safe /
  // operational-target / statutory) as the policy label; render it directly.
  const labels: Record<string, string> = {};
  return (
    <div className="rounded-md border border-info/40 bg-info/5 p-3">
      <p className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
        Retirement age — earliest-safe · target · statutory
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {(derivation.readiness_by_policy ?? []).map((v) => (
          <div
            key={v.policy}
            className="rounded border border-border/60 p-2"
            title={v.rationale}
          >
            <p className="text-[10px] text-muted-foreground">
              {labels[v.policy] ?? v.policy}
            </p>
            <p className="text-lg font-semibold font-mono">
              {v.retire_ready_age == null
                ? "—"
                : `age ${v.retire_ready_age.toFixed(0)}`}
            </p>
          </div>
        ))}
      </div>
      <p className="text-[11px] text-muted-foreground mt-2">
        Earliest-safe is the earliest age the Monte Carlo clears 90% solvency
        with the finite-liability reserve earmarked (sequence-of-returns aware).
        Target is the plan&apos;s operating age; statutory is the pension/BL age.
        Hover any tile for the backing numbers. Return-sensitivity lives on
        the /retirement μ-grid.
      </p>
    </div>
  );
}

function SensitivityStrip({
  derivation,
}: {
  derivation: HeadlineDerivationDTO;
}) {
  const baseMu = derivation.mu_nominal_annual;
  return (
    <div className="rounded-md border border-warning/40 bg-warning/5 p-3">
      <p className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
        How fragile is this? — retire age at different expected returns
      </p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {derivation.sensitivity_by_mu.map(([mu, age], i) => {
          const isBase = Math.abs(mu - baseMu) < 1e-6;
          return (
            <div
              key={`mu-${i}`}
              className={
                isBase
                  ? "rounded border border-primary/60 bg-primary/10 p-2 text-center"
                  : "rounded border border-border/60 p-2 text-center"
              }
            >
              <p className="text-[10px] text-muted-foreground">
                if μ = {(mu * 100).toFixed(0)}%
                {isBase ? " (base)" : ""}
              </p>
              <p className="text-lg font-semibold font-mono">
                {age == null
                  ? "—"
                  : Number.isFinite(age)
                    ? `age ${age.toFixed(0)}`
                    : "—"}
              </p>
            </div>
          );
        })}
      </div>
      <p className="text-[11px] text-muted-foreground mt-2">
        μ is the expected nominal portfolio return per year. The base
        case calibrates σ from your portfolio composition; other
        knobs (tax, inflation, drift) are held constant in this sweep.
        A 2-point swing in μ moves the retire age by several years —
        the number is real but the precision isn&apos;t. Use the
        cashflow chart sliders to stress-test other knobs.
      </p>
    </div>
  );
}
