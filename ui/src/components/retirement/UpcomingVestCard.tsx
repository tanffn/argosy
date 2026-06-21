"use client";

import Link from "next/link";
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type AllocationProposalDTO,
  type UpcomingVestDTO,
  type UpcomingVestOutlookDTO,
} from "@/lib/api";

interface Props {
  userId: string;
  horizonDays?: number;
}

/**
 * Sprint #2 commit #12 — RSU pre-vest planning card.
 *
 * Per spec §3.3 of
 * docs/superpowers/specs/2026-05-29-anomaly-detection-rsu-prevest-design.md:
 *
 *   - Header per upcoming vest: "Vesting in N days: <shares> NVDA shares
 *     (grant <id>)" + expected vest date.
 *   - Body: estimated gross USD + three-scenario post-tax estimates
 *     (nominal / effective / conservative) + allocation preview built
 *     off the NOMINAL post-tax amount.
 *   - Footnote: explicit "Nominal = plan-assumed X%, effective = filed
 *     Y%, conservative = max(47%, nominal+5%)." so the user sees which
 *     rate drives the headline number.
 *   - Footer: "Add as life event →" CTA that pre-fills the /life-events
 *     form with category=asset_event, kind=other_asset_acquired,
 *     target_date=expected_vest_date, amount_usd=post-tax-nominal,
 *     description="RSU vest from grant <id>".
 *
 * Empty-state shapes:
 *   - No upcoming vests in horizon → "no upcoming vests" line with a
 *     nudge toward /life-events. Keeps the surface present so the
 *     /retirement page layout doesn't shift around when a user has
 *     just zero pending vests.
 *   - API error → error banner; no fabricated numbers.
 */
export function UpcomingVestCard({ userId, horizonDays = 90 }: Props) {
  const [data, setData] = useState<UpcomingVestOutlookDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Loading + error reset happen inside the async callback so the
    // effect body doesn't trigger a cascading render. Initial render
    // already starts with loading=true / error=null; subsequent
    // (userId, horizonDays) changes flip back to "loading" via the
    // .then/.catch boundary below.
    api.retirement
      .upcomingVests(userId, horizonDays)
      .then((d) => {
        if (cancelled) return;
        setError(null);
        setData(d);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setData(null);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId, horizonDays]);

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Upcoming RSU vests</CardTitle>
          <CardDescription className="flex items-center gap-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Projecting the next {horizonDays} days of expected tranches…
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (error || data === null) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Upcoming RSU vests</CardTitle>
          <CardDescription>
            Couldn&apos;t load the outlook: {error ?? "unknown error"}.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (data.upcoming.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Upcoming RSU vests</CardTitle>
          <CardDescription>
            No projected vests in the next {data.horizon_days} days. Once
            a historical Schwab vest lands, this card will project the
            next per-grant tranche at +90d.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Upcoming RSU vests</CardTitle>
        <CardDescription>
          Projected per-grant vest events in the next{" "}
          <span className="font-mono">{data.horizon_days}</span> days.
          Three-scenario tax estimate + allocation preview based on the{" "}
          <span className="italic">nominal</span> post-tax amount.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {data.upcoming.map((v, idx) => (
          <VestRow key={`${v.grant_id}-${v.expected_vest_date}-${idx}`} vest={v} />
        ))}
        <RateFootnote
          nominal={data.rate_nominal}
          effective={data.rate_effective}
          conservative={data.rate_conservative}
        />
      </CardContent>
    </Card>
  );
}

function VestRow({ vest }: { vest: UpcomingVestDTO }) {
  const sharesLabel = vest.shares_projected.toLocaleString(undefined, {
    maximumFractionDigits: 2,
  });
  const grossLabel = formatUsd(vest.expected_gross_usd);
  const postNominal = formatUsd(vest.expected_post_tax_nominal_usd);
  const postEffective = formatUsd(vest.expected_post_tax_effective_usd);
  const postConservative = formatUsd(vest.expected_post_tax_conservative_usd);

  const prefillHref = buildLifeEventHref(vest);

  return (
    <div className="border border-border rounded-md p-3 flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-sm font-semibold tabular-nums">
          Vesting in {vest.days_until} day{vest.days_until === 1 ? "" : "s"}:
        </span>
        <span className="text-sm font-mono">
          {sharesLabel} NVDA shares
        </span>
        <span className="text-xs text-muted-foreground font-mono">
          (grant {vest.grant_id})
        </span>
        <span className="ml-auto text-xs text-muted-foreground font-mono">
          {vest.expected_vest_date}
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Estimated gross
          </div>
          <div className="mt-1 text-xl font-mono font-semibold tabular-nums">
            {grossLabel}
          </div>
          <div className="text-[11px] text-muted-foreground tabular-nums">
            @ ${vest.nvda_price_usd.toLocaleString(undefined, {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            })}/share spot
          </div>
        </div>

        <div className="flex flex-col gap-1">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Estimated post-tax
          </div>
          <ScenarioRow
            label="nominal"
            rate={vest.rate_nominal}
            value={postNominal}
            highlight
          />
          <ScenarioRow
            label="effective"
            rate={vest.rate_effective}
            value={postEffective}
          />
          <ScenarioRow
            label="conservative"
            rate={vest.rate_conservative}
            value={postConservative}
          />
        </div>
      </div>

      {vest.allocation_preview.length > 0 ? (
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            If this lands as cash today, suggested allocation
          </div>
          <ul className="mt-1 flex flex-col gap-1">
            {vest.allocation_preview.map((p, idx) => (
              <AllocationPreviewRow key={`${p.asset_class}-${p.instrument}-${idx}`} proposal={p} />
            ))}
          </ul>
        </div>
      ) : (
        <div className="text-[11px] text-muted-foreground italic">
          No allocation preview — needs a portfolio snapshot with a
          plan-target block. Upload a Family Finances Status TSV to
          populate.
        </div>
      )}

      <div className="flex justify-end">
        <Link
          href={prefillHref}
          className="text-xs text-info hover:underline"
        >
          Discuss in Advisor →
        </Link>
      </div>
    </div>
  );
}

function ScenarioRow({
  label,
  rate,
  value,
  highlight,
}: {
  label: string;
  rate: number;
  value: string;
  highlight?: boolean;
}) {
  return (
    <div
      className={
        "flex items-baseline gap-2 text-sm" +
        (highlight ? " font-semibold" : " text-muted-foreground")
      }
    >
      <span className="text-[10px] uppercase tracking-wider w-20">
        {label}
      </span>
      <span className="font-mono tabular-nums">{value}</span>
      <span className="text-[10px] font-mono">
        @ {(rate * 100).toFixed(1)}%
      </span>
    </div>
  );
}

function AllocationPreviewRow({ proposal }: { proposal: AllocationProposalDTO }) {
  return (
    <li className="text-xs flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <span className="font-mono tabular-nums">
        {formatUsd(proposal.amount_usd)}
      </span>
      <span className="text-muted-foreground">→</span>
      <span className="font-mono">{proposal.asset_class}</span>
      <span className="text-muted-foreground">/</span>
      <span className="font-mono">{proposal.instrument}</span>
    </li>
  );
}

function RateFootnote({
  nominal,
  effective,
  conservative,
}: {
  nominal: number;
  effective: number;
  conservative: number;
}) {
  return (
    <p className="text-[11px] text-muted-foreground border-t border-border pt-2">
      <span className="font-semibold">Nominal</span> ={" "}
      <span className="font-mono">{(nominal * 100).toFixed(1)}%</span>{" "}
      plan-assumed marginal rate;{" "}
      <span className="font-semibold">effective</span> ={" "}
      <span className="font-mono">{(effective * 100).toFixed(1)}%</span>{" "}
      your prior-year filed rate;{" "}
      <span className="font-semibold">conservative</span> ={" "}
      <span className="font-mono">{(conservative * 100).toFixed(1)}%</span>{" "}
      = max(47%, nominal + 5%) — the supplemental-withholding worst
      case. The allocation preview uses the{" "}
      <span className="italic">nominal</span> post-tax amount.
    </p>
  );
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function formatUsd(n: number): string {
  return "$" + n.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  });
}

// The vest-planning CTA opens an Advisor conversation pre-seeded with
// the vest details so the user can talk through the cashflow impact in
// chat. Replaces the previous /life-events form prefill — the dedicated
// Life Events page was removed in the 2026-05-31 wave; the advisor is
// now the entry point for capturing cashflow events.
//
// Defensive: when `expected_post_tax_nominal_usd` is missing or
// non-positive (tax-rate bug, missing FMV), open Advisor with a
// generic vest-discussion seed instead of a wrong-amount one.
function buildLifeEventHref(vest: UpcomingVestDTO): string {
  const raw = vest.expected_post_tax_nominal_usd;
  const hasAmount = Number.isFinite(raw) && raw > 0;
  const amountClause = hasAmount
    ? ` Expected post-tax cash: ~$${Math.round(raw).toLocaleString()}.`
    : "";
  const seed =
    `RSU vest from grant ${vest.grant_id} expected on ${vest.expected_vest_date}.` +
    amountClause +
    ` Help me think through what to do with the cash and how it changes my plan.`;
  return `/advisor?seed=${encodeURIComponent(seed)}`;
}
