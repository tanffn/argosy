"use client";

import { useEffect, useState } from "react";

import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { HeroCard } from "@/components/retirement/HeroCard";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type AllocationBreakdownDTO,
  type WindfallActionListItem,
  type WindfallDetectResponse,
  type WindfallEventDTO,
  type WindfallProposalDTO,
} from "@/lib/api";
import type { ValueWithRationale } from "@/lib/retirement-types";

// Single-user-mode binding. Matches the USER_ID convention used across
// the retirement components. When multi-tenant lands, this lifts to a
// session-derived value via auth context.
const USER_ID = "ariel";

/**
 * Full-surface card for the auto-detected cash-position change on
 * /retirement.
 *
 * Renders the same auto-detected event as <WindfallBanner> on Home, but
 * with the entire allocation plan visible: hero verdict + a source
 * breakdown (what's explained by sales vs the unexplained residual) +
 * 8-row allocation delta table + 3 horizon proposal cards
 * (long/medium/short) with Accept/Defer buttons.
 *
 * This is NOT framed as a "windfall"/gift: the cash delta is a
 * month-over-month change in the cash position to be allocated, and the
 * card surfaces its likely SOURCE rather than implying free money.
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
          <CardTitle className="text-base">Cash-change detector</CardTitle>
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
      <CashSourceBreakdown event={event} />
      <AllocationDeltaTable />
      <ProposalsGrid plan={plan} event={event} />

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
          (5% tolerance). The plan splits the cash to allocate 60/25/15
          across long/medium/short horizons; long-term picks tickers you
          already hold to close the biggest plan-target gaps.
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
  const hasSales = event.matching_sales.length > 0;

  // Title + verdict never imply free money — this is a cash-position
  // change to allocate, with its likely SOURCE named.
  const title = hasSales
    ? "Cash position changed — to allocate"
    : "Unexplained cash change — review & allocate";

  // Prefer the backend allocator's canonical headline rationale (it
  // reasons over classification + plan-gap priorities + allocator
  // confidence). Fall back to source-driven copy that frames the delta
  // honestly when the backend doesn't supply one.
  const verdict =
    plan?.headline?.rationale ??
    (hasSales
      ? `Includes ${saleNames(event.matching_sales)} sale(s); allocate per the plan below.`
      : "No matching sale this month — likely an in-month reallocation. Review the source before allocating.");

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
      title={title}
      status={status}
      verdict={verdict}
      numbers={[
        {
          label: "Cash to allocate",
          display: formatUsd(event.cash_delta_total_usd_equiv),
          secondary: `${formatUsd(event.cash_delta_usd)} USD + ${formatNis(event.cash_delta_nis)} NIS @ ₪${event.fx_usd_nis.toFixed(2)}/$`,
          children: (
            <ValueWithTooltip
              display={formatUsd(event.cash_delta_total_usd_equiv)}
              data={{
                value: event.cash_delta_total_usd_equiv,
                unit: "USD",
                source_id: event.source_tsv,
                rationale: `Difference between the cash + USD bank rows in ${humanTsvLabel(event.source_tsv)} vs ${humanTsvLabel(event.previous_tsv ?? "")}. NIS leg converted at the snapshot's FX (₪${event.fx_usd_nis.toFixed(4)}/$). This is a month-over-month change in the cash position to allocate — not a gift.`,
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

/**
 * Compact source breakdown for the cash delta — answers "where did the
 * $X come from?" so the headline isn't an unexplained number. Splits the
 * total into what matched equity sales explain vs the unexplained
 * residual, using only data already in the response (matching_sales).
 */
function CashSourceBreakdown({ event }: { event: WindfallEventDTO }) {
  const total = event.cash_delta_total_usd_equiv;
  const matched = event.matching_sales.reduce((acc, s) => acc + s.value_usd, 0);
  const residual = total - matched;
  // Mirror the detector's 5% match band: a residual under 5% of the
  // total is rounding/FX noise, not a separate unexplained source.
  const hasResidual = Math.abs(residual) > 0.05 * Math.abs(total);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base font-mono">
          What produced the {formatUsd(total)} change
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="space-y-1.5 text-sm font-mono tabular-nums">
          {event.matching_sales.map((s) => (
            <li
              key={s.symbol}
              className="flex items-center justify-between gap-3 border-b border-border/20 pb-1.5"
            >
              <span>
                {s.symbol} {Math.abs(s.shares_sold).toLocaleString()} sh sold
                <span className="text-muted-foreground">
                  {" "}
                  @ ${s.current_price.toFixed(2)}
                </span>
              </span>
              <span className="text-info">{formatUsd(s.value_usd)}</span>
            </li>
          ))}
          {event.matching_sales.length === 0 ? (
            <li className="flex items-center justify-between gap-3 border-b border-border/20 pb-1.5">
              <span className="text-muted-foreground">
                No matching equity sale this month
              </span>
              <span className="text-muted-foreground">$0</span>
            </li>
          ) : null}
          {hasResidual ? (
            <li className="flex items-center justify-between gap-3 pt-0.5">
              <span className="text-amber-400">
                Unexplained residual — likely a reallocation
              </span>
              <span className="text-amber-400">
                {formatUsd(Math.abs(residual))}
              </span>
            </li>
          ) : null}
        </ul>
        <p className="mt-3 text-[11px] text-muted-foreground leading-relaxed">
          {event.matching_sales.length === 0
            ? `No sale in ${humanTsvLabel(event.source_tsv)} matched the cash delta. A cash change with no matching gift or sale is most likely an in-month reallocation — review the source before treating it as new money.`
            : hasResidual
              ? `Matched sales explain ${formatUsd(matched)} of the ${formatUsd(total)} change; the remaining ${formatUsd(Math.abs(residual))} is unexplained (most likely an in-month reallocation). This is cash to allocate, not a gift.`
              : `The matched sale(s) account for the full ${formatUsd(total)} change. This is cash to allocate, not a gift.`}
        </p>
      </CardContent>
    </Card>
  );
}

function AllocationDeltaTable() {
  // Rebound to the CANONICAL source (codex / user 2026-06-12): this table now
  // reads /api/portfolio/allocation-breakdown — the SAME canonical plan targets
  // the /portfolio card shows — instead of the windfall detector's TSV targets,
  // so the two surfaces can never disagree. Only classes with a plan target are
  // shown (they're the deployment destinations).
  const [data, setData] = useState<AllocationBreakdownDTO | null>(null);
  useEffect(() => {
    api.portfolioAllocationBreakdown().then(setData).catch(() => {});
  }, []);
  if (!data) return null;
  const rows = data.rows
    .filter((r) => r.target_pct !== null)
    .map((r) => ({
      asset_class: r.label,
      current_pct: r.current_pct / 100,
      target_pct: (r.target_pct ?? 0) / 100,
      delta_k_usd: (((r.target_pct ?? 0) - r.current_pct) / 100) * data.total_value_k,
    }));
  const sorted = [...rows].sort((a, b) => b.delta_k_usd - a.delta_k_usd);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base font-mono">
          Where new cash would go &mdash; vs plan target
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
                // Tone semantics: a windfall is cash looking for a
                // destination, so "under target" rows are the positive
                // signal (where the money should flow) — render as
                // accent/info, not warning. "Over target" rows aren't
                // a success either; a windfall doesn't trim, it adds.
                // Neutral keeps the focus on the under-target rows.
                const tone: "accent" | "neutral" = isCash
                  ? "neutral"
                  : under
                    ? "accent"
                    : "neutral";
                const label = isCash
                  ? "CASH"
                  : under
                    ? "DESTINATION"
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
          DESTINATION rows are under target — the natural home for the
          cash to allocate. OVER rows are above target (trimming, not
          adding) and aren&apos;t destinations. Cash is excluded by
          construction — the delta is already cash, so allocating cash to
          cash is a no-op.
        </p>
      </CardContent>
    </Card>
  );
}

interface ProposalsGridProps {
  plan: NonNullable<WindfallDetectResponse["plan"]>;
  event: WindfallEventDTO;
}

function ProposalsGrid({ plan, event }: ProposalsGridProps) {
  // Pre-fetch any existing windfall_actions rows for this event so each
  // ProposalRow can render its already-decided state inline (Accepted ✓
  // / Deferred until ...) instead of showing fresh Accept/Defer
  // buttons. Keyed by event.source_tsv since detected_at can drift
  // by milliseconds on re-fetches.
  const [existing, setExisting] = useState<WindfallActionListItem[]>([]);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .windfallActionsList(USER_ID, event.source_tsv)
      .then((r) => {
        if (!cancelled) setExisting(r.actions);
      })
      .catch(() => {
        // Swallow -- the page renders fine without prior decisions;
        // we just won't pre-populate the per-proposal status.
      });
    return () => {
      cancelled = true;
    };
  }, [event.source_tsv]);

  const onDecided = (action: WindfallActionListItem) => {
    setExisting((rows) => [...rows, action]);
  };

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
      <ProposalColumn
        title="Long term"
        budgetFraction="60%"
        proposals={plan.long_term}
        emptyHint="No under-target asset classes — windfall would sit in cash."
        event={event}
        existing={existing}
        onDecided={onDecided}
      />
      <ProposalColumn
        title="Medium term"
        budgetFraction="25%"
        proposals={plan.medium_term}
        emptyHint="Awaiting agent-fleet synthesis."
        event={event}
        existing={existing}
        onDecided={onDecided}
      />
      <ProposalColumn
        title="Short term"
        budgetFraction="15%"
        proposals={plan.short_term}
        emptyHint="Awaiting watchlist + news scan."
        event={event}
        existing={existing}
        onDecided={onDecided}
      />
    </div>
  );
}

interface ProposalColumnProps {
  title: string;
  budgetFraction: string;
  proposals: WindfallProposalDTO[];
  emptyHint: string;
  event: WindfallEventDTO;
  existing: WindfallActionListItem[];
  onDecided: (action: WindfallActionListItem) => void;
}

function ProposalColumn({
  title,
  budgetFraction,
  proposals,
  emptyHint,
  event,
  existing,
  onDecided,
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
          proposals.map((p, i) => {
            // Match a prior decision on the (horizon, asset_class,
            // instrument, amount_usd) tuple -- this uniquely identifies
            // a proposal in a plan, and the allocator copies these
            // verbatim on Accept/Defer so the match is exact.
            const prior = existing.find(
              (a) =>
                a.horizon === p.horizon &&
                a.asset_class === p.asset_class &&
                a.instrument === p.instrument &&
                Math.abs(a.amount_usd - p.amount_usd) < 0.01,
            );
            return (
              <ProposalRow
                key={`${p.horizon}-${i}`}
                proposal={p}
                event={event}
                prior={prior}
                onDecided={onDecided}
              />
            );
          })
        )}
      </CardContent>
    </Card>
  );
}

interface ProposalRowProps {
  proposal: WindfallProposalDTO;
  event: WindfallEventDTO;
  prior: WindfallActionListItem | undefined;
  onDecided: (action: WindfallActionListItem) => void;
}

function ProposalRow({ proposal, event, prior, onDecided }: ProposalRowProps) {
  const isPlaceholder =
    proposal.instrument.startsWith("<") || proposal.instrument.endsWith(">");
  const [busy, setBusy] = useState<"accept" | "defer" | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  const submit = async (verb: "accept" | "defer") => {
    setBusy(verb);
    setError(null);
    try {
      const payload = {
        user_id: USER_ID,
        event_detected_at: event.detected_at,
        event_source_tsv: event.source_tsv,
        horizon: proposal.horizon,
        asset_class: proposal.asset_class,
        instrument: proposal.instrument,
        amount_usd: proposal.amount_usd,
        rationale: proposal.rationale,
        closes_delta_usd: proposal.closes_delta_usd,
        confidence: proposal.confidence,
      };
      const resp =
        verb === "accept"
          ? await api.retirement.windfallAccept(payload)
          : await api.retirement.windfallDefer(payload);
      onDecided({
        id: resp.id,
        event_detected_at: payload.event_detected_at,
        event_source_tsv: payload.event_source_tsv,
        horizon: payload.horizon,
        asset_class: payload.asset_class,
        instrument: payload.instrument,
        amount_usd: payload.amount_usd,
        decided_status: resp.decided_status,
        decided_at: resp.decided_at,
        due_date: resp.due_date,
        user_note: null,
        proposal_id: null,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
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
      <div className="mt-1 flex items-center gap-2 flex-wrap">
        {prior ? (
          <span
            className={`font-mono text-[11px] px-2 py-0.5 rounded border ${
              prior.decided_status === "accepted"
                ? "border-emerald-400/40 bg-emerald-400/10 text-emerald-400"
                : "border-amber-400/40 bg-amber-400/10 text-amber-400"
            }`}
            title={`Recorded ${prior.decided_at}`}
          >
            {prior.decided_status === "accepted" ? "✓ Accepted" : "↻ Deferred"}
            {prior.due_date ? ` · due ${prior.due_date}` : ""}
          </span>
        ) : (
          <>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => void submit("accept")}
              className={`font-mono text-[11px] px-2 py-0.5 rounded border border-emerald-400/40 bg-emerald-400/10 text-emerald-400 hover:bg-emerald-400/20 transition-colors ${
                busy !== null ? "opacity-60 cursor-wait" : "cursor-pointer"
              }`}
            >
              {busy === "accept" ? "…" : "Accept"}
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => void submit("defer")}
              className={`font-mono text-[11px] px-2 py-0.5 rounded border border-amber-400/40 bg-amber-400/10 text-amber-400 hover:bg-amber-400/20 transition-colors ${
                busy !== null ? "opacity-60 cursor-wait" : "cursor-pointer"
              }`}
            >
              {busy === "defer" ? "…" : "Defer"}
            </button>
          </>
        )}
        {error ? (
          <span className="font-mono text-[11px] text-rose-400">
            {error}
          </span>
        ) : null}
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
    event.cash_delta_total_usd_equiv === 0 || totalSalesValue === 0
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

// Human-readable join of matched sale symbols, e.g. "BRK.B" or
// "BRK.B + AAPL". Used in the hero verdict to name the cash source.
function saleNames(sales: { symbol: string }[]): string {
  return sales.map((s) => s.symbol).join(" + ");
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
