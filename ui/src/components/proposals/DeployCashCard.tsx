"use client";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  api,
  type AllocationActionListItem,
  type AllocationActionRequest,
  type DeploymentLineDTO,
  type DeploymentMarketContextDTO,
  type DeploymentPlanDTO,
  type DeploymentTierDTO,
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

export function DeployCashCard({
  plan,
  loading,
  amount,
  onAmountChange,
  unallocatedUsd,
  userId,
  live = false,
  onLiveChange,
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
}) {
  // Prior allocation decisions, keyed by source_ref, so each buy line can
  // render its Accepted/Deferred pill inline. Shares the "unallocated_cash"
  // action_source with the UnallocatedCashCard — one ledger, two surfaces.
  const [decisions, setDecisions] = useState<
    Map<string, AllocationActionListItem>
  >(new Map());

  const planAsOf = plan?.as_of ?? "";

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
