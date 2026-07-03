"use client";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  api,
  type AllocationActionListItem,
  type AllocationActionRequest,
  type AuthoredAllocationDTO,
  type DeploymentLineDTO,
  type DeploymentDispositionDTO,
  type DeploymentMarketContextDTO,
  type DeploymentPlanDTO,
  type DeploymentTierDTO,
  type PreflightDTO,
  type WindfallHorizon,
} from "@/lib/api";

const TIER_LABEL: Record<string, string> = {
  reserve: "Reserve",
  core: "Core",
  medium: "Medium",
  high: "High",
};

// Size-proportional rounding for DISPLAY only (the actual order keeps the exact
// value). A small buy rounds to a fine step, a big one to a coarse step, so the
// numbers read clean without distorting meaningfully: a $3k line snaps to $500,
// a $120k line to $5k. Step grows with magnitude.
function niceRound(n: number): number {
  const abs = Math.abs(n);
  let step: number;
  if (abs < 1_000) step = 50;
  else if (abs < 10_000) step = 500;
  else if (abs < 100_000) step = 1_000;
  else step = 5_000;
  return Math.round(n / step) * step;
}

// Format a USD amount as a clean, size-proportional figure: under $10k in full
// ("$6,500"), $10k+ in compact "k" notation ("$52k", "$120k").
function fmtMoney(n: number): string {
  const r = niceRound(n);
  if (Math.abs(r) >= 10_000) return `$${Math.round(r / 1_000).toLocaleString()}k`;
  return `$${r.toLocaleString()}`;
}

// The deploy plan carries a free-form per-line horizon string plus a risk
// tier; the allocation_actions store only accepts the windfall horizon enum
// (long/medium/short). Map the line to one of those: prefer an explicit
// long/medium/short signal in line.horizon, else derive from the risk tier
// (reserve/core → long-term holds; medium → medium; high → short, the most
// tactical sleeve). Falls back to "long" so a line is never unmappable.
function lineHorizon(line: DeploymentLineDTO): WindfallHorizon {
  const h = (line.horizon ?? "").toLowerCase();
  if (h.includes("short")) return "short";
  if (h.includes("medium") || h.includes("mid")) return "medium";
  if (h.includes("long")) return "long";
  switch (line.tier) {
    case "high":
      return "short";
    case "medium":
      return "medium";
    default:
      return "long";
  }
}

/** Same (snapshot, horizon, asset_class, instrument) source_ref identity the
 *  UnallocatedCashCard uses, so a buy accepted from either surface dedups at
 *  the DB layer. snapshot_date here is the deploy plan's as_of. */
function buildSourceRef(args: {
  snapshotDate: string | null;
  horizon: string;
  assetClass: string;
  instrument: string;
}): string {
  return JSON.stringify({
    snapshot_date: args.snapshotDate,
    horizon: args.horizon,
    asset_class: args.assetClass,
    instrument: args.instrument,
  });
}

// ---------------------------------------------------------------------------
// P2: MarketContextStrip — surfaces live macro snapshot + freshness + NVDA
// verification. Rendered only when plan.market_context is present (i.e. when
// the caller requested ?live=true).
// ---------------------------------------------------------------------------

/** Format age_seconds into a human-readable "N min ago" / "Nh ago" string. */
function fmtAge(seconds: number): string {
  if (seconds < 120) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${(seconds / 3600).toFixed(1)}h ago`;
}

const SNAPSHOT_LABELS: Record<string, string> = {
  sp500: "S&P 500",
  vix: "VIX",
  usd_nis: "USD/NIS",
  boi_rate: "BoI Rate",
  oil_wti: "Oil (WTI)",
  cpi_yoy: "CPI YoY",
  sp_vs_trend_pct: "S&P vs 200-day MA (%)",
  fed_funds: "Fed Funds (%)",
  ust10: "US 10y (%)",
  real10: "US 10y real (%)",
  breakeven10: "10y breakeven (%)",
  ig_spread: "IG spread (%)",
  hy_spread: "HY spread (%)",
};

function MarketContextStrip({ ctx }: { ctx: DeploymentMarketContextDTO }) {
  const staleAnywhere =
    ctx.is_any_stale ||
    ctx.freshness.some((f) => f.is_stale) ||
    ctx.nvda?.consistent === false;

  return (
    <div
      className="mt-3 rounded border border-border/60 bg-muted/20 p-3 text-xs"
      data-testid="market-context-strip"
    >
      <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
        <span className="font-semibold text-sm">Live market context</span>
        <span className="text-muted-foreground">{ctx.overall_age_label}</span>
        {staleAnywhere && (
          <span
            className="rounded bg-red-600 px-2 py-0.5 font-bold text-white"
            data-testid="stale-badge"
          >
            STALE DATA
          </span>
        )}
      </div>

      {/* Macro snapshot values */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 mb-2">
        {Object.entries(ctx.snapshot).map(([key, val]) => (
          <span key={key}>
            <span className="text-muted-foreground">
              {SNAPSHOT_LABELS[key] ?? key}:{" "}
            </span>
            <span className="font-mono">{Number(val).toLocaleString()}</span>
          </span>
        ))}
      </div>

      {/* Per-field freshness */}
      {ctx.freshness.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-0.5 mb-2 text-muted-foreground">
          {ctx.freshness.map((f) => (
            <span key={f.field} className={f.is_stale ? "text-red-500 font-semibold" : ""}>
              {SNAPSHOT_LABELS[f.field] ?? f.field}: {fmtAge(f.age_seconds)}
              {f.is_stale && " ⚠"}
            </span>
          ))}
        </div>
      )}

      {/* NVDA verification */}
      {ctx.nvda && (
        <div
          className={`border-t border-border/40 pt-2 mt-1 ${
            ctx.nvda.consistent === false ? "text-red-500 font-semibold" : ""
          }`}
          data-testid="nvda-verification"
        >
          <span className="font-semibold">NVDA:</span>{" "}
          <span className="font-mono">${ctx.nvda.price.toLocaleString()}</span>
          {ctx.nvda.shares !== null && (
            <span className="ml-2 text-muted-foreground">
              {(ctx.nvda.shares / 1e9).toFixed(2)}B shares
            </span>
          )}
          {ctx.nvda.consistent === false && (
            <span className="ml-2 text-red-500 font-bold">INCONSISTENT ⚠</span>
          )}
          {ctx.nvda.consistent === true && (
            <span className="ml-2 text-green-600">verified ✓</span>
          )}
          {ctx.nvda.note && (
            <span className="ml-2 text-muted-foreground">— {ctx.nvda.note}</span>
          )}
        </div>
      )}
    </div>
  );
}

function TierHeading({ tier }: { tier: DeploymentTierDTO }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-sm font-semibold tracking-tight">
        {TIER_LABEL[tier.name]}
      </span>
      <span className="text-sm text-muted-foreground tabular-nums">
        {fmtMoney(tier.total_usd)}
      </span>
    </div>
  );
}

interface TierBlockProps {
  tier: DeploymentTierDTO;
  userId: string;
  planAsOf: string;
  decisions: Map<string, AllocationActionListItem>;
  onDecided: (sourceRef: string, action: AllocationActionListItem) => void;
}

function TierBlock({
  tier,
  userId,
  planAsOf,
  decisions,
  onDecided,
}: TierBlockProps) {
  if (tier.lines.length === 0) {
    return (
      <div className="mt-4">
        <TierHeading tier={tier} />
        <div className="text-sm text-muted-foreground mt-1">
          {tier.name === "core" ? "—" : "Populated in a later phase."}
        </div>
      </div>
    );
  }
  // Guarantee priority order within the tier: biggest allocation first (Core
  // fills the largest plan-target gaps first; the High sleeve is sized by
  // conviction, so highest conviction leads).
  const lines = [...tier.lines].sort((a, b) => b.amount_usd - a.amount_usd);
  return (
    <div className="mt-4">
      <TierHeading tier={tier} />
      <div className="mt-2 divide-y divide-border/40 rounded-md border border-border/50">
        {lines.map((l) => {
          const sourceRef = buildSourceRef({
            snapshotDate: planAsOf,
            horizon: lineHorizon(l),
            assetClass: l.tier,
            instrument: l.symbol,
          });
          return (
            <div
              key={`${tier.name}-${l.symbol}`}
              className="flex items-start gap-3 px-3 py-2.5"
            >
              {/* Symbol + type */}
              <div className="w-24 shrink-0">
                <div className="font-semibold leading-tight">{l.symbol}</div>
                <div className="text-xs text-muted-foreground">{l.type}</div>
              </div>

              {/* Amount + NEW/ADD */}
              <div className="w-28 shrink-0">
                <div className="font-semibold tabular-nums leading-tight">
                  {fmtMoney(l.amount_usd)}
                </div>
                <span
                  title={
                    l.is_new
                      ? "Opens a position you don't currently hold"
                      : "Adds to a position you already hold"
                  }
                  className={`mt-0.5 inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                    l.is_new
                      ? "bg-emerald-500/15 text-emerald-600"
                      : "bg-sky-500/15 text-sky-600"
                  }`}
                >
                  {l.is_new ? "New" : "Add"}
                </span>
              </div>

              {/* Reason + estate + timing */}
              <div className="min-w-0 flex-1">
                <div className="text-sm leading-snug">
                  {l.cap_note}
                  {l.rationale ? ` — ${l.rationale}` : ""}
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
                  <span>{l.estate.status.replace(/_/g, " ")}</span>
                  <span aria-hidden>·</span>
                  <span>{l.timing}</span>
                  {l.pace_rationale && (
                    <>
                      <span aria-hidden>·</span>
                      <span data-testid={`pace-rationale-${l.symbol}`}>
                        {l.pace_rationale}
                      </span>
                    </>
                  )}
                </div>
              </div>

              {/* Decision */}
              <div className="shrink-0">
                <DeployLineActions
                  line={l}
                  userId={userId}
                  planAsOf={planAsOf}
                  sourceRef={sourceRef}
                  prior={decisions.get(sourceRef) ?? null}
                  onDecided={(action) => onDecided(sourceRef, action)}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface DeployLineActionsProps {
  line: DeploymentLineDTO;
  userId: string;
  planAsOf: string;
  sourceRef: string;
  prior: AllocationActionListItem | null;
  onDecided: (action: AllocationActionListItem) => void;
}

/**
 * Per-line Accept/Defer for the deploy buy list. Persists through the SAME
 * allocation_actions flow the UnallocatedCashCard uses (action_source
 * "unallocated_cash", identical AllocationActionRequest shape + source_ref
 * convention) so the two surfaces share one decision ledger and a buy
 * accepted on either shows its pill on both.
 */
function DeployLineActions({
  line,
  userId,
  planAsOf,
  sourceRef,
  prior,
  onDecided,
}: DeployLineActionsProps) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const horizon = lineHorizon(line);

  const submit = async (status: "accepted" | "deferred") => {
    if (busy) return;
    setBusy(true);
    setErr(null);
    const payload: AllocationActionRequest = {
      user_id: userId,
      action_source: "unallocated_cash",
      // The deploy plan is computed from the latest snapshot; approximate the
      // detection time with the plan's as_of date, falling back to now.
      source_detected_at: planAsOf
        ? `${planAsOf}T00:00:00Z`
        : new Date().toISOString(),
      source_ref: sourceRef,
      horizon,
      asset_class: line.tier,
      instrument: line.symbol,
      amount_usd: line.amount_usd,
      rationale: line.rationale,
      closes_delta_usd: line.amount_usd,
      confidence: "medium",
    };
    try {
      const fn =
        status === "accepted"
          ? api.proposalAllocationAccept
          : api.proposalAllocationDefer;
      const resp = await fn(payload);
      onDecided({
        id: resp.id,
        action_source: "unallocated_cash",
        source_detected_at: payload.source_detected_at,
        source_ref: sourceRef,
        horizon,
        asset_class: line.tier,
        instrument: line.symbol,
        amount_usd: line.amount_usd,
        decided_status: resp.decided_status,
        decided_at: resp.decided_at,
        due_date: resp.due_date,
        user_note: null,
        proposal_id: null,
      });
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (prior) {
    return (
      <Badge
        variant={prior.decided_status === "accepted" ? "success" : "secondary"}
        className="text-[11px] whitespace-nowrap"
      >
        {prior.decided_status === "accepted"
          ? `✓ Accepted at ${new Date(prior.decided_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
          : `↻ Deferred${prior.due_date ? ` · due ${prior.due_date}` : ""}`}
      </Badge>
    );
  }

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <Button
        size="sm"
        variant="outline"
        disabled={busy}
        onClick={() => submit("accepted")}
        className="h-7 text-[11px]"
      >
        Accept
      </Button>
      <Button
        size="sm"
        variant="ghost"
        disabled={busy}
        onClick={() => submit("deferred")}
        className="h-7 text-[11px]"
      >
        Defer
      </Button>
      {err && <span className="text-rose-400 text-[11px]">{err}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Fleet-authors / determinism-verifies pivot: render the AUTHORED allocation.
// When accepted it is the PRIMARY recommendation; the deterministic tiers are
// demoted to a collapsed reference. When degraded (rejected/unavailable) a loud
// banner explains why and the deterministic `tiers` become the labelled
// fallback (their normal rendering).
// ---------------------------------------------------------------------------

/** Format a claimed look-through US weight (0..1 or 0..100) as a percent. */
function fmtUsWeight(w: number): string {
  const pct = w <= 1 ? w * 100 : w;
  return `${Math.round(pct)}%`;
}

/** The accepted, verifier-approved fleet-authored allocation — shown as the
 *  primary recommendation. Read-only for now: the per-line Accept/Defer ledger
 *  still lives on the (demoted) deterministic tiers below. */
function AuthoredAllocationBlock({ authored }: { authored: AuthoredAllocationDTO }) {
  const buys = [...authored.buys].sort((a, b) => b.amount_usd - a.amount_usd);
  return (
    <div
      data-testid="authored-allocation"
      className="mt-3 rounded-md border border-emerald-300 bg-emerald-50/60 p-3 dark:border-emerald-900 dark:bg-emerald-950/30"
    >
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="text-sm font-semibold text-emerald-800 dark:text-emerald-300">
          Argosy&apos;s authored allocation
        </div>
        <span
          data-testid="authored-verified-badge"
          title="The fleet authored this allocation; the deterministic verifier approved it."
          className="rounded bg-emerald-600 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white"
        >
          Fleet-authored · verifier-approved
        </span>
      </div>

      <div className="mt-1 text-xs text-muted-foreground">
        {`Deploying ${fmtMoney(authored.cash_to_deploy)}`}
        {authored.cash_to_reserve >= 1 &&
          ` · holding ${fmtMoney(authored.cash_to_reserve)} as cash`}
      </div>

      {authored.rationale && (
        <p className="mt-2 text-xs leading-snug text-foreground/90">
          {authored.rationale}
        </p>
      )}

      {/* Buys */}
      {buys.length > 0 && (
        <div className="mt-2 divide-y divide-emerald-200/50 rounded-md border border-emerald-200/60 dark:divide-emerald-900/50 dark:border-emerald-900/60">
          {buys.map((b) => (
            <div
              key={`buy-${b.symbol}`}
              className="flex items-start gap-3 px-3 py-2"
              data-testid={`authored-buy-${b.symbol}`}
            >
              <div className="w-24 shrink-0">
                <div className="font-semibold leading-tight">{b.symbol}</div>
                {b.sleeve && (
                  <div className="text-xs text-muted-foreground">{b.sleeve}</div>
                )}
              </div>
              <div className="w-28 shrink-0">
                <div className="font-semibold tabular-nums leading-tight">
                  {fmtMoney(b.amount_usd)}
                </div>
                <span
                  title={
                    b.is_new
                      ? "Opens a position you don't currently hold"
                      : "Adds to a position you already hold"
                  }
                  className={`mt-0.5 inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                    b.is_new
                      ? "bg-emerald-500/15 text-emerald-600"
                      : "bg-sky-500/15 text-sky-600"
                  }`}
                >
                  {b.is_new ? "New" : "Add"}
                </span>
              </div>
              <div className="min-w-0 flex-1">
                {/* Per-line reason: the author's justification, falling back to the
                    sleeve so the column is never blank. */}
                <div className="text-sm leading-snug">
                  {b.justification || b.sleeve || "—"}
                </div>
                {b.claimed_us_weight !== null && (
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {`Look-through US weight: ${fmtUsWeight(b.claimed_us_weight)}`}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Sells (off-plan trims) */}
      {authored.sells.length > 0 && (
        <div className="mt-2" data-testid="authored-sells">
          <div className="text-xs font-semibold text-amber-700 dark:text-amber-400">
            Proposed trims
          </div>
          <ul className="mt-1 space-y-1 text-xs">
            {authored.sells.map((s) => (
              <li key={`sell-${s.symbol}`} className="flex gap-2">
                <span className="w-24 shrink-0 font-medium">
                  {s.symbol} · {fmtMoney(s.amount_usd)}
                </span>
                <span className="text-muted-foreground">{s.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Holds */}
      {authored.holds.length > 0 && (
        <div className="mt-2 text-xs text-muted-foreground" data-testid="authored-holds">
          {`Holding unchanged: ${authored.holds.join(", ")}`}
        </div>
      )}

      {authored.notes.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-[11px] text-muted-foreground">
          {authored.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Loud, honest banner shown when the author could NOT produce a verifier-passing
 *  allocation (rejected) or was unavailable (timeout / circuit-open / no backend).
 *  The deterministic `tiers` below are then the labelled fallback. */
function DegradedAuthorBanner({ authored }: { authored: AuthoredAllocationDTO }) {
  const why =
    authored.notes.find((n) => n.toLowerCase().includes("degraded")) ||
    (authored.status === "unavailable"
      ? "The fleet author was unavailable (timeout / circuit-open / no backend)."
      : "The fleet author could not produce an allocation that passed the deterministic verifier.");
  return (
    <div
      data-testid="authored-degraded-banner"
      className="mt-3 rounded-md border border-amber-400 bg-amber-50/70 p-3 dark:border-amber-800 dark:bg-amber-950/30"
    >
      <div className="flex items-center gap-2">
        <span className="rounded bg-amber-500 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
          Degraded
        </span>
        <span className="text-xs font-semibold text-amber-800 dark:text-amber-300">
          Showing the deterministic engine&apos;s allocation (fallback)
        </span>
      </div>
      <p className="mt-1.5 text-xs text-foreground/90">{why}</p>
      {authored.gate_failures.length > 0 && (
        <ul className="mt-1.5 list-disc pl-5 text-[11px] text-muted-foreground">
          {authored.gate_failures.map((f, i) => (
            <li key={i}>
              <span className="font-medium">{f.code}</span>
              {f.detail ? `: ${f.detail}` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function DeployCashCard({
  plan,
  loading,
  amount,
  onAmountChange,
  unallocatedUsd,
  userId,
  live = false,
  onLiveChange,
  onRunFleetReview,
  fleetReviewing = false,
}: {
  plan: DeploymentPlanDTO | null;
  loading: boolean;
  amount: number;
  onAmountChange: (v: number) => void;
  unallocatedUsd: number;
  /** User whose allocation_actions back the per-line Accept/Defer. */
  userId: string;
  /** P2: whether to request live market context. Default false (P1 behavior). */
  live?: boolean;
  /** P2: called when the user toggles the live-market-context checkbox. */
  onLiveChange?: (v: boolean) => void;
  /** Phase 2: run the agent fleet on the "pending fleet judgment" items. */
  onRunFleetReview?: () => void;
  /** Phase 2 in flight (the fleet call takes minutes). */
  fleetReviewing?: boolean;
}) {
  // Prior allocation decisions, keyed by source_ref, so each buy line can
  // render its Accepted/Deferred pill inline. Shares the "unallocated_cash"
  // action_source with the UnallocatedCashCard — one ledger, two surfaces.
  const [decisions, setDecisions] = useState<
    Map<string, AllocationActionListItem>
  >(new Map());

  const planAsOf = plan?.as_of ?? "";
  // Pivot: the fleet-authored allocation is the primary recommendation only when
  // it was accepted by the deterministic verifier. Otherwise (degraded / null)
  // the deterministic tiers stay primary.
  const authoredAccepted =
    plan?.authored != null &&
    plan.authored.status === "accepted" &&
    !plan.authored.degraded;

  useEffect(() => {
    let cancelled = false;
    api
      .proposalAllocationActionsList(userId, { actionSource: "unallocated_cash" })
      .then((resp) => {
        if (cancelled) return;
        const next = new Map<string, AllocationActionListItem>();
        for (const a of resp.actions) {
          if (a.source_ref) next.set(a.source_ref, a);
        }
        setDecisions(next);
      })
      .catch(() => {
        /* swallow — pills just don't render */
      });
    return () => {
      cancelled = true;
    };
  }, [userId, planAsOf]);

  const onDecided = (sourceRef: string, action: AllocationActionListItem) => {
    setDecisions((prev) => {
      const next = new Map(prev);
      next.set(sourceRef, action);
      return next;
    });
  };

  return (
    <section className="rounded-lg border p-4">
      <h2 className="text-lg font-semibold">Deploy Cash</h2>
      <div className="text-sm text-muted-foreground">
        {`Unallocated cash: ${fmtMoney(unallocatedUsd)}`}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-4">
        <label className="block text-sm">
          Amount to deploy (net of tax)
          <input
            type="number"
            value={amount}
            onChange={(e) => onAmountChange(Number(e.target.value))}
            className="ml-2 rounded border px-2 py-1"
          />
        </label>
        {onLiveChange !== undefined && (
          <label className="flex items-center gap-1.5 text-sm cursor-pointer select-none">
            <input
              type="checkbox"
              checked={live}
              onChange={(e) => onLiveChange(e.target.checked)}
              data-testid="live-toggle"
            />
            Live market context
          </label>
        )}
      </div>
      {loading && <div className="mt-3 text-sm">Computing…</div>}
      {!loading && plan && (
        <>
          {plan.note && (
            <div className="mt-2 text-sm italic">{plan.note}</div>
          )}
          <div className="mt-2 text-sm">
            <span>{`Deployed: ${fmtMoney(plan.deployed_total_usd)}`}</span>
            {/* Hide a sub-$1 rounding-artifact remainder; show real shortfalls. */}
            {plan.undeployed_remainder_usd >= 1 && (
              <span className="ml-3 text-amber-600">
                {`Undeployed remainder: ${fmtMoney(plan.undeployed_remainder_usd)}`}
              </span>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            {`US-situs estate exposure (planned buys): ${fmtMoney(plan.us_situs_exposed_usd)}`}
            {plan.us_situs_sanctioned_usd > 0 &&
              ` · sanctioned NVDA sleeve: ${fmtMoney(plan.us_situs_sanctioned_usd)}`}
          </div>
          {plan.market_context && (
            <MarketContextStrip ctx={plan.market_context} />
          )}
          {plan.disposition && <DispositionBlock d={plan.disposition} />}
          {plan.preflight && (
            <PreflightVerdict
              preflight={plan.preflight}
              onRunFleetReview={onRunFleetReview}
              fleetReviewing={fleetReviewing}
            />
          )}
          {/* Fleet-authors / determinism-verifies pivot. */}
          {authoredAccepted && plan.authored && (
            <AuthoredAllocationBlock authored={plan.authored} />
          )}
          {plan.authored && !authoredAccepted && (
            <DegradedAuthorBanner authored={plan.authored} />
          )}
          {/* Deterministic tiers: primary normally; a collapsed reference once
              the fleet-authored allocation is the accepted recommendation. */}
          {authoredAccepted ? (
            <details className="mt-4" data-testid="deterministic-tiers-details">
              <summary className="cursor-pointer text-sm text-muted-foreground">
                Deterministic engine&apos;s allocation (reference — not the
                recommendation)
              </summary>
              <div>
                {plan.tiers.map((t) => (
                  <TierBlock
                    key={t.name}
                    tier={t}
                    userId={userId}
                    planAsOf={planAsOf}
                    decisions={decisions}
                    onDecided={onDecided}
                  />
                ))}
              </div>
            </details>
          ) : (
            plan.tiers.map((t) => (
              <TierBlock
                key={t.name}
                tier={t}
                userId={userId}
                planAsOf={planAsOf}
                decisions={decisions}
                onDecided={onDecided}
              />
            ))
          )}
          <ul className="mt-3 list-disc pl-5 text-xs text-muted-foreground">
            {plan.caveats.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

const PREFLIGHT_STATUS_STYLE: Record<string, { label: string; cls: string }> = {
  veto: { label: "Vetoed", cls: "text-red-600" },
  cap_at_pct: { label: "Capped", cls: "text-amber-600" },
  defer: { label: "Deferred", cls: "text-muted-foreground" },
  requires_plan_change: { label: "Needs plan change", cls: "text-amber-600" },
  move_to_reserve: { label: "To reserve", cls: "text-muted-foreground" },
  approve_candidate: { label: "OK", cls: "text-emerald-600" },
  needs_fleet_review: { label: "Needs fleet judgment", cls: "text-sky-600" },
};

const DISPOSITION_ACTION_STYLE: Record<string, { label: string; cls: string }> = {
  deploy: { label: "Deploy", cls: "text-emerald-600" },
  hold_cash: { label: "Hold as cash", cls: "text-muted-foreground" },
  deconcentrate_first: { label: "Trim NVDA first", cls: "text-amber-600" },
  raise_plan_change: { label: "Add to plan", cls: "text-sky-600" },
};

/** The fleet's affirmative answer to "what should I DO with this cash?" — the
 *  headline recommendation covering the full amount (deploy / hold-with-reason /
 *  deconcentrate / plan-change). Present only after a phase-2 fleet review. */
function DispositionBlock({ d }: { d: DeploymentDispositionDTO }) {
  return (
    <div className="mt-3 rounded-md border border-sky-300 bg-sky-50/60 p-3 dark:border-sky-900 dark:bg-sky-950/30">
      <div className="text-xs font-semibold text-sky-800 dark:text-sky-300">
        Argosy&apos;s recommendation (fleet)
      </div>
      <p className="mt-1 text-xs text-foreground/90">{d.summary}</p>
      <ul className="mt-2 space-y-1 text-xs">
        {d.items.map((it, i) => {
          const s = DISPOSITION_ACTION_STYLE[it.action] ?? {
            label: it.action,
            cls: "text-muted-foreground",
          };
          return (
            <li key={i} className="flex flex-col gap-0.5">
              <div className="flex gap-2">
                <span className={`w-28 shrink-0 font-medium ${s.cls}`}>
                  {s.label}
                </span>
                <span className="font-medium">
                  {it.target} · {fmtMoney(it.amount_usd)}
                </span>
              </div>
              <span className="ml-28 text-[11px] text-muted-foreground">
                {it.reason}
              </span>
            </li>
          );
        })}
      </ul>
      {d.confidence && (
        <div className="mt-1 text-[11px] text-muted-foreground">
          Fleet confidence: {d.confidence.toLowerCase()}
        </div>
      )}
    </div>
  );
}

/** Research verdict on the deterministic buy list: concentration/reserve checks
 *  per line + any plan questions. Advisory (shadow) — annotates the list above. */
function PreflightVerdict({
  preflight,
  onRunFleetReview,
  fleetReviewing = false,
}: {
  preflight: PreflightDTO;
  onRunFleetReview?: () => void;
  fleetReviewing?: boolean;
}) {
  const flagged = preflight.enriched.filter(
    (e) => e.status !== "approve_candidate",
  );
  // Items the fast pass surfaced as "pending fleet judgment" — resolved by the
  // phase-2 "Run fleet review" action.
  const pending = flagged.filter((e) => e.status === "needs_fleet_review");
  if (flagged.length === 0 && preflight.plan_gaps.length === 0) return null;
  return (
    <div className="mt-3 rounded-md border border-border bg-muted/30 p-3">
      <div className="flex items-center justify-between">
        <div className="text-xs font-medium">Research check</div>
        {pending.length > 0 && onRunFleetReview && (
          <Button
            size="sm"
            variant="secondary"
            disabled={fleetReviewing}
            onClick={onRunFleetReview}
          >
            {fleetReviewing
              ? "Fleet reviewing… (minutes)"
              : `Run fleet review (${pending.length}) →`}
          </Button>
        )}
      </div>
      {pending.length > 0 && (
        <div className="mt-1 text-[11px] text-muted-foreground">
          {pending.length} item{pending.length > 1 ? "s" : ""} need the agent
          fleet&apos;s judgment (RiskOfficer + FundManager) before deploying —
          the facts are below.
        </div>
      )}
      {flagged.length > 0 && (
        <ul className="mt-1 space-y-1 text-xs">
          {flagged.map((e) => {
            const s = PREFLIGHT_STATUS_STYLE[e.status] ?? {
              label: e.status,
              cls: "text-muted-foreground",
            };
            return (
              <li key={e.symbol} className="flex flex-col gap-0.5">
                <div className="flex gap-2">
                  <span className={`w-28 shrink-0 font-medium ${s.cls}`}>
                    {e.symbol}: {s.label}
                  </span>
                  <span className="text-muted-foreground">{e.reason}</span>
                </div>
                {(e.flags ?? []).length > 0 && (
                  <ul className="ml-28 list-disc pl-4 text-[11px] text-muted-foreground">
                    {(e.flags ?? []).map((f, i) => (
                      <li key={i}>{f.fact}</li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {preflight.plan_gaps.map((g) => (
        <div key={g.asset_class} className="mt-2 text-xs text-amber-700">
          {`Your plan has no "${g.asset_class}" sleeve — intended, or worth adding? `}
          {g.reason_refs[0] ?? ""}
        </div>
      ))}
      {preflight.notes.map((n, i) => (
        <div key={i} className="mt-1 text-[11px] text-muted-foreground">
          {n}
        </div>
      ))}
    </div>
  );
}
