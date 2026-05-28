"use client";

import { useEffect, useState } from "react";

import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { HeroCard } from "@/components/retirement/HeroCard";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type WindfallAllocationLineDTO,
  type WindfallClassifiedSource,
  type WindfallDetectResponse,
  type WindfallEventDTO,
  type WindfallProposalDTO,
} from "@/lib/api";
import type { ValueWithRationale } from "@/lib/retirement-types";

/**
 * Full-surface windfall card for /retirement.
 *
 * Renders the same auto-detected event as <WindfallBanner> on Home, but
 * with the entire allocation plan visible: hero verdict + 8-row allocation
 * delta table + 3 horizon proposal cards (long/medium/short) with
 * Accept/Defer buttons.
 *
 * Buttons are disabled in this iteration — Accept/Defer wiring through
 * action_engine is deferred to a follow-up; see the resume note at
 * docs/superpowers/plans/2026-05-28-windfall-flow-resume.md.
 *
 * Suppresses entirely when no event is detected, mirroring the banner.
 */
export function WindfallCard() {
  const [data, setData] = useState<WindfallDetectResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .windfallDetect()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (err) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Windfall detector</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-rose-400 font-mono">
          Failed to fetch: {err}
        </CardContent>
      </Card>
    );
  }

  // Hide the surface entirely when there's no windfall — same UX as the
  // banner. We don't render a "no windfall" stub because /retirement is
  // already a long page and a phantom card would be visual noise.
  if (!data) return null;
  if (!data.event || !data.plan) return null;

  const event = data.event;
  const plan = data.plan;

  return (
    <div className="space-y-3">
      <WindfallHero event={event} plan={plan} />
      <AllocationDeltaTable rows={event.allocation_delta_table} />
      <ProposalsGrid plan={plan} />

      <DrilldownSection title="Matching equity sales (same month)">
        <SalesTable event={event} />
      </DrilldownSection>

      <DrilldownSection title="How this works">
        <p className="text-sm text-muted-foreground leading-relaxed">
          Argosy compares the two most-recent monthly Family Finances Status
          TSVs in your Portfolio/Resources folder. When the cash delta
          crosses <span className="font-mono">$25K USD</span> or{" "}
          <span className="font-mono">₪75K NIS</span>, it tags the event and
          classifies the source by matching equity sales in the same month
          (5% tolerance). The plan splits the windfall 60/25/15 across
          long/medium/short horizons; long-term picks tickers you already
          hold to close the biggest plan-target gaps.
        </p>
        <p className="mt-2 text-sm text-muted-foreground leading-relaxed">
          Medium-term and short-term proposals are placeholders — the agent
          fleet (analysts → bull/bear → trader → 3 risk officers → FM) hasn&apos;t
          been wired in yet for those horizons.
        </p>
      </DrilldownSection>
    </div>
  );
}

interface WindfallHeroProps {
  event: WindfallEventDTO;
  plan: WindfallDetectResponse["plan"];
}

function WindfallHero({ event, plan }: WindfallHeroProps) {
  const status = event.requires_user_classification ? "WARN" : "UNCERTAIN";
  const verdict = event.requires_user_classification
    ? "Source unclear — likely cash was redeployed in-month. Confirm classification before allocating."
    : `Classified as ${classificationLabel(event.classified_source)}. Review the proposed allocation below.`;

  const longTotal = (plan?.long_term ?? []).reduce(
    (acc, p) => acc + p.amount_usd,
    0,
  );
  const mediumTotal = (plan?.medium_term ?? []).reduce(
    (acc, p) => acc + p.amount_usd,
    0,
  );
  const shortTotal = (plan?.short_term ?? []).reduce(
    (acc, p) => acc + p.amount_usd,
    0,
  );

  return (
    <HeroCard
      title="Windfall detected"
      status={status}
      verdict={verdict}
      numbers={[
        {
          label: "Cash delta",
          display: formatUsd(event.cash_delta_total_usd_equiv),
          secondary: `${formatUsd(event.cash_delta_usd)} USD + ${formatNis(event.cash_delta_nis)} NIS @ ₪${event.fx_usd_nis.toFixed(2)}/$`,
          children: (
            <ValueWithTooltip
              display={formatUsd(event.cash_delta_total_usd_equiv)}
              data={{
                value: event.cash_delta_total_usd_equiv,
                unit: "USD",
                source_id: event.source_tsv,
                rationale: `Difference between the cash + USD bank rows in ${humanTsvLabel(event.source_tsv)} vs ${humanTsvLabel(event.previous_tsv ?? "")}. NIS leg converted at the snapshot's FX (₪${event.fx_usd_nis.toFixed(4)}/$).`,
                confidence: "high",
              }}
            />
          ),
        },
        {
          label: "Long-term budget",
          display: formatUsd(longTotal),
          secondary: `${(plan?.long_term ?? []).length} proposal${(plan?.long_term ?? []).length === 1 ? "" : "s"} closing plan gaps`,
        },
        {
          label: "Medium + short pending",
          display: formatUsd(mediumTotal + shortTotal),
          secondary: "Agent-fleet synthesis not wired up yet",
        },
      ]}
      subline={
        <span>
          Source: {humanTsvLabel(event.source_tsv)} · compared against{" "}
          {humanTsvLabel(event.previous_tsv ?? "")}
        </span>
      }
    />
  );
}

interface AllocationDeltaTableProps {
  rows: WindfallAllocationLineDTO[];
}

function AllocationDeltaTable({ rows }: AllocationDeltaTableProps) {
  const sorted = [...rows].sort((a, b) => b.delta_k_usd - a.delta_k_usd);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base font-mono">
          Current allocation vs plan target
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-sm font-mono tabular-nums">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-muted-foreground border-b border-border/40">
                <th className="text-left py-1.5 pr-3">Asset class</th>
                <th className="text-right py-1.5 px-2">Current</th>
                <th className="text-right py-1.5 px-2">Target</th>
                <th className="text-right py-1.5 px-2">Δ (target − current)</th>
                <th className="text-right py-1.5 pl-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((row) => {
                const isCash = row.asset_class.trim().toLowerCase() === "cash";
                const under = row.delta_k_usd > 0;
                const over = row.delta_k_usd < 0;
                const tone: "warning" | "success" | "neutral" = isCash
                  ? "neutral"
                  : under
                    ? "warning"
                    : over
                      ? "success"
                      : "neutral";
                const label = isCash
                  ? "CASH"
                  : under
                    ? "UNDER"
                    : over
                      ? "OVER"
                      : "ON";
                return (
                  <tr
                    key={row.asset_class}
                    className="border-b border-border/20 last:border-0"
                  >
                    <td className="py-1.5 pr-3">{row.asset_class}</td>
                    <td className="text-right py-1.5 px-2">
                      {(row.current_pct * 100).toFixed(1)}%
                    </td>
                    <td className="text-right py-1.5 px-2">
                      {(row.target_pct * 100).toFixed(1)}%
                    </td>
                    <td className="text-right py-1.5 px-2">
                      {row.delta_k_usd > 0 ? "+" : ""}
                      ${Math.round(row.delta_k_usd).toLocaleString()}K
                    </td>
                    <td className="text-right py-1.5 pl-2">
                      <StatusPill tone={tone} mono>
                        {label}
                      </StatusPill>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-[11px] text-muted-foreground">
          Positive Δ = under target (room to buy). Negative Δ = over target
          (room to trim). Cash is excluded from windfall destinations —
          allocating a cash windfall to cash is a no-op.
        </p>
      </CardContent>
    </Card>
  );
}

interface ProposalsGridProps {
  plan: NonNullable<WindfallDetectResponse["plan"]>;
}

function ProposalsGrid({ plan }: ProposalsGridProps) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
      <ProposalColumn
        title="Long term"
        budgetFraction="60%"
        proposals={plan.long_term}
        emptyHint="No under-target asset classes — windfall would sit in cash."
      />
      <ProposalColumn
        title="Medium term"
        budgetFraction="25%"
        proposals={plan.medium_term}
        emptyHint="Awaiting agent-fleet synthesis."
      />
      <ProposalColumn
        title="Short term"
        budgetFraction="15%"
        proposals={plan.short_term}
        emptyHint="Awaiting watchlist + news scan."
      />
    </div>
  );
}

interface ProposalColumnProps {
  title: string;
  budgetFraction: string;
  proposals: WindfallProposalDTO[];
  emptyHint: string;
}

function ProposalColumn({
  title,
  budgetFraction,
  proposals,
  emptyHint,
}: ProposalColumnProps) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm font-mono">{title}</CardTitle>
          <StatusPill tone="neutral" mono>
            {budgetFraction}
          </StatusPill>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {proposals.length === 0 ? (
          <p className="text-xs text-muted-foreground font-mono">{emptyHint}</p>
        ) : (
          proposals.map((p, i) => (
            <ProposalRow key={`${p.horizon}-${i}`} proposal={p} />
          ))
        )}
      </CardContent>
    </Card>
  );
}

function ProposalRow({ proposal }: { proposal: WindfallProposalDTO }) {
  const isPlaceholder =
    proposal.instrument.startsWith("<") || proposal.instrument.endsWith(">");

  // Tooltip data wraps the dollar amount with the proposal's rationale so
  // the user gets the "why" without having to scroll. source_id is
  // "argosy_derived" for these — not an external citation, hence null on
  // the source link.
  const tooltipData: ValueWithRationale = {
    value: proposal.amount_usd,
    unit: "USD",
    source_id: null,
    rationale: proposal.rationale,
    confidence: proposal.confidence,
  };

  return (
    <div className="rounded-md border border-border/50 bg-secondary/30 px-3 py-2 flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <span className="font-mono text-sm font-semibold">
          {isPlaceholder ? proposal.asset_class : proposal.instrument}
        </span>
        <ValueWithTooltip
          display={formatUsd(proposal.amount_usd)}
          data={tooltipData}
          className="font-mono text-sm"
        />
      </div>
      {!isPlaceholder ? (
        <div className="text-[11px] text-muted-foreground">
          {proposal.asset_class} ·{" "}
          <span className={confidenceClass(proposal.confidence)}>
            {proposal.confidence}
          </span>{" "}
          confidence
        </div>
      ) : (
        <div className="text-[11px] text-muted-foreground italic">
          {proposal.rationale}
        </div>
      )}
      <div className="mt-1 flex items-center gap-2">
        <button
          type="button"
          disabled
          title="Accept/Defer wiring to action_engine is deferred — coming in a follow-up."
          className="font-mono text-[11px] px-2 py-0.5 rounded border border-border/60 bg-background/40 text-muted-foreground/70 cursor-not-allowed"
        >
          Accept
        </button>
        <button
          type="button"
          disabled
          title="Accept/Defer wiring to action_engine is deferred — coming in a follow-up."
          className="font-mono text-[11px] px-2 py-0.5 rounded border border-border/60 bg-background/40 text-muted-foreground/70 cursor-not-allowed"
        >
          Defer
        </button>
      </div>
    </div>
  );
}

function SalesTable({ event }: { event: WindfallEventDTO }) {
  if (event.matching_sales.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No equity sales in {humanTsvLabel(event.source_tsv)} matched the cash
        delta within 5%. The classifier tagged this event as{" "}
        <span className="font-mono">{event.classified_source}</span> for that
        reason — most likely a deposit, bonus, or in-month redeployment.
      </p>
    );
  }
  const totalSalesValue = event.matching_sales.reduce(
    (acc, s) => acc + s.value_usd,
    0,
  );
  const matchRatio =
    event.cash_delta_total_usd_equiv === 0
      ? 0
      : (event.cash_delta_total_usd_equiv / totalSalesValue) * 100;
  return (
    <div className="space-y-2">
      <table className="w-full text-sm font-mono tabular-nums">
        <thead>
          <tr className="text-[10px] uppercase tracking-wider text-muted-foreground border-b border-border/40">
            <th className="text-left py-1.5 pr-3">Symbol</th>
            <th className="text-right py-1.5 px-2">Shares sold</th>
            <th className="text-right py-1.5 px-2">@ Price</th>
            <th className="text-right py-1.5 pl-2">Value (USD)</th>
          </tr>
        </thead>
        <tbody>
          {event.matching_sales.map((s) => (
            <tr
              key={s.symbol}
              className="border-b border-border/20 last:border-0"
            >
              <td className="py-1.5 pr-3">{s.symbol}</td>
              <td className="text-right py-1.5 px-2">
                {Math.abs(s.shares_sold).toLocaleString()}
              </td>
              <td className="text-right py-1.5 px-2">
                ${s.current_price.toFixed(2)}
              </td>
              <td className="text-right py-1.5 pl-2">
                ${Math.round(s.value_usd).toLocaleString()}
              </td>
            </tr>
          ))}
          <tr className="border-t border-border/40 font-semibold">
            <td className="py-1.5 pr-3" colSpan={3}>
              Total sales value
            </td>
            <td className="text-right py-1.5 pl-2">
              ${Math.round(totalSalesValue).toLocaleString()}
            </td>
          </tr>
        </tbody>
      </table>
      <p className="text-[11px] text-muted-foreground">
        Cash delta is {matchRatio.toFixed(0)}% of total sales value. When this
        ratio is between 95% and 105%, the classifier auto-tags the source.
        Outside that band, it flags the event as <span className="font-mono">unclear</span>{" "}
        — most likely a chunk of the proceeds was redeployed inside the same
        month.
      </p>
    </div>
  );
}

// ---------- formatters --------------------------------------------------

function formatUsd(value: number): string {
  if (!Number.isFinite(value)) return "$—";
  return `$${Math.round(value).toLocaleString()}`;
}

function formatNis(value: number): string {
  if (!Number.isFinite(value)) return "₪—";
  return `₪${Math.round(value).toLocaleString()}`;
}

function classificationLabel(source: WindfallClassifiedSource): string {
  switch (source) {
    case "rsu_sale":
      return "RSU sale";
    case "stock_sale":
      return "stock sale";
    default:
      return "unclear";
  }
}

function confidenceClass(c: "high" | "medium" | "low"): string {
  switch (c) {
    case "high":
      return "text-emerald-400";
    case "medium":
      return "text-amber-400";
    default:
      return "text-muted-foreground";
  }
}

function humanTsvLabel(filename: string): string {
  if (!filename) return "—";
  const m = filename.match(/(\d{2})\s+([A-Za-z]{3})/);
  if (!m) return filename;
  const [, yy, mon] = m;
  const year = 2000 + Number(yy);
  return `${mon} ${year}`;
}
