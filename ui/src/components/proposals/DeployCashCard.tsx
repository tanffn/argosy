"use client";
import type {
  DeploymentMarketContextDTO,
  DeploymentPlanDTO,
  DeploymentTierDTO,
} from "@/lib/api";

const TIER_LABEL: Record<string, string> = {
  reserve: "Reserve",
  core: "Core",
  medium: "Medium",
  high: "High",
};

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
    <div className="font-semibold">
      <span>{TIER_LABEL[tier.name]}</span>
      {` ($${tier.total_usd.toLocaleString()})`}
    </div>
  );
}

function TierBlock({ tier }: { tier: DeploymentTierDTO }) {
  if (tier.lines.length === 0) {
    return (
      <div className="mt-3">
        <TierHeading tier={tier} />
        <div className="text-sm text-muted-foreground">
          {tier.name === "core" ? "—" : "Populated in a later phase."}
        </div>
      </div>
    );
  }
  return (
    <div className="mt-3">
      <TierHeading tier={tier} />
      <table className="w-full text-sm">
        <thead>
          <tr>
            <th>SYMBOL</th>
            <th>TYPE</th>
            <th>AMOUNT</th>
            <th>TIMING</th>
            <th>NEW?</th>
            <th>ESTATE</th>
            <th>REASON</th>
          </tr>
        </thead>
        <tbody>
          {tier.lines.map((l) => (
            <tr key={`${tier.name}-${l.symbol}`}>
              <td>{l.symbol}</td>
              <td>{l.type}</td>
              <td>{`$${l.amount_usd.toLocaleString()}`}</td>
              <td>
                <div>{l.timing}</div>
                {l.pace_rationale && (
                  <div
                    className="text-xs text-muted-foreground"
                    data-testid={`pace-rationale-${l.symbol}`}
                  >
                    {l.pace_rationale}
                  </div>
                )}
              </td>
              <td>{l.is_new ? "NEW" : "held"}</td>
              <td>{l.estate.status.replace(/_/g, " ")}</td>
              <td>
                {l.cap_note}
                {l.rationale ? ` — ${l.rationale}` : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DeployCashCard({
  plan,
  loading,
  amount,
  onAmountChange,
  unallocatedUsd,
  live = false,
  onLiveChange,
}: {
  plan: DeploymentPlanDTO | null;
  loading: boolean;
  amount: number;
  onAmountChange: (v: number) => void;
  unallocatedUsd: number;
  /** P2: whether to request live market context. Default false (P1 behavior). */
  live?: boolean;
  /** P2: called when the user toggles the live-market-context checkbox. */
  onLiveChange?: (v: boolean) => void;
}) {
  return (
    <section className="rounded-lg border p-4">
      <h2 className="text-lg font-semibold">Deploy Cash</h2>
      <div className="text-sm text-muted-foreground">
        {`Unallocated cash: $${unallocatedUsd.toLocaleString()}`}
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
            <span>{`Deployed: $${plan.deployed_total_usd.toLocaleString()}`}</span>
            {plan.undeployed_remainder_usd > 0 && (
              <span className="ml-3 text-amber-600">
                {`Undeployed remainder: $${plan.undeployed_remainder_usd.toLocaleString()}`}
              </span>
            )}
          </div>
          <div className="text-xs text-muted-foreground">
            {`US-situs estate exposure (planned buys): $${plan.us_situs_exposed_usd.toLocaleString()}`}
            {plan.us_situs_sanctioned_usd > 0 &&
              ` · sanctioned NVDA sleeve: $${plan.us_situs_sanctioned_usd.toLocaleString()}`}
          </div>
          {plan.market_context && (
            <MarketContextStrip ctx={plan.market_context} />
          )}
          {plan.tiers.map((t) => (
            <TierBlock key={t.name} tier={t} />
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
