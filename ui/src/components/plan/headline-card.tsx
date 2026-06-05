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
  type DualTrackPlanResponse,
  type DualTrackTrack,
  type HeadlineDerivationDTO,
  type RecapSummaryDTO,
} from "@/lib/api";
import { formatLocalDateTime } from "@/lib/utils";

interface HeadlineCardProps {
  recap: RecapSummaryDTO;
  // The /plan page's USER_ID, threaded down so the readiness panel can
  // self-fetch the dual-track plan (the engine that actually produces
  // the two ages) for the at-95 estate table + assumptions collapsible.
  userId: string;
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

export function HeadlineCard({ recap, userId }: HeadlineCardProps) {
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

        {recap.derivation && recap.derivation.sensitivity_by_mu.length > 0 ? (
          <SensitivityStrip derivation={recap.derivation} />
        ) : null}

        {recap.derivation?.readiness_by_policy &&
        recap.derivation.readiness_by_policy.length > 0 ? (
          <ReadinessByPolicyStrip
            derivation={recap.derivation}
            userId={userId}
          />
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
// Split a "WHY: … WHAT IT MEANS: …" rationale into its two labeled parts so the
// hover panel can render them as a clean why / implication pair.
function splitRationale(rationale: string): { why: string; means: string | null } {
  const marker = "WHAT IT MEANS:";
  const i = rationale.indexOf(marker);
  if (i === -1) return { why: rationale.replace(/^WHY:\s*/i, "").trim(), means: null };
  return {
    why: rationale.slice(0, i).replace(/^WHY:\s*/i, "").trim(),
    means: rationale.slice(i + marker.length).trim(),
  };
}

// Format a NIS amount as ₪X.XM / ₪Xk / ₪X. Mirrors the project's "money in
// plain English" convention; null/non-finite renders as an em-dash.
function formatNis(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (abs >= 1_000_000) return `${sign}₪${(abs / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${sign}₪${Math.round(abs / 1_000)}k`;
  return `${sign}₪${Math.round(abs)}`;
}

// Read a numeric key out of the dual-track ``assumptions`` bag (values are
// number | string). Returns null when absent or not a finite number so the
// caller can skip the row instead of rendering NaN.
function assumpNum(
  bag: Record<string, number | string>,
  key: string,
): number | null {
  const v = bag[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function assumpStr(
  bag: Record<string, number | string>,
  key: string,
): string | null {
  const v = bag[key];
  return typeof v === "string" ? v : null;
}

function pct(value: number | null, digits = 0): string {
  return value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

// One regime's at-95 estate reading for a single retire-age. ``solvent`` is the
// p_solvent_95 in [0,1]; ``honest`` flags the BEAR row when its solvency falls
// below the 85% comfort line, so the table renders it in amber/rose.
interface EstateRegimeRow {
  name: DualTrackTrack["name"];
  label: string;
  medianRealNis: number;
  worst10RealNis: number;
  solvent: number;
  honest: boolean;
}

// Pull the at-95 estate readings for one tile age across all three tracks by
// finding each track's frontier point whose ``retire_age === age``. Returns []
// when nothing matches (e.g. the age is outside the swept frontier), which the
// caller treats as "no enrichment for this tile".
function estateRowsForAge(
  plan: DualTrackPlanResponse,
  age: number,
): EstateRegimeRow[] {
  const rows: EstateRegimeRow[] = [];
  for (const track of plan.tracks) {
    const point = track.frontier.find((p) => p.retire_age === age);
    if (point == null) continue;
    rows.push({
      name: track.name,
      label: track.label,
      medianRealNis: point.median_estate_real_nis,
      worst10RealNis: point.worst10_estate_real_nis,
      solvent: point.p_solvent_95,
      honest: track.name === "bear" && point.p_solvent_95 < 0.85,
    });
  }
  return rows;
}

function ReadinessByPolicyStrip({
  derivation,
  userId,
}: {
  derivation: HeadlineDerivationDTO;
  userId: string;
}) {
  // Two COMPUTED tracks only (drawdown + capital-preservation). The planned
  // target is an input → shown as a caption, not a co-equal tile.
  const tracks = derivation.readiness_by_policy ?? [];
  const target = derivation.retirement_target_age;

  // Self-fetch the dual-track plan (the engine that actually produces these two
  // ages). 1000 paths keeps the per-request cost responsive; the enrichment is
  // strictly additive — the two tiles render immediately from the recap, and a
  // fetch failure silently hides only the estate table + assumptions block.
  const [plan, setPlan] = useState<DualTrackPlanResponse | null>(null);
  const [enrichState, setEnrichState] = useState<"loading" | "ready" | "error">(
    "loading",
  );

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- justified: userId-driven fetch; toggling the loading flag inside the effect is the whole point
    setEnrichState("loading");
    api.retirement
      .dualTrackPlan(userId, 1000)
      .then((r) => {
        if (cancelled) return;
        setPlan(r);
        setEnrichState("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setPlan(null);
        setEnrichState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  // The two tile ages, in tile order (drawdown first, capital-preservation
  // second), with the policy label so the estate sub-table can title itself.
  const tileAges = tracks
    .filter((v) => v.retire_ready_age != null)
    .map((v) => ({
      label: v.policy,
      age: Math.round(v.retire_ready_age as number),
    }));

  return (
    <div className="rounded-md border border-info/40 bg-info/5 p-3">
      <p className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
        When can you retire? — spend it down vs. leave it to the kids
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {tracks.map((v) => {
          const { why, means } = splitRationale(v.rationale ?? "");
          return (
            <div
              key={v.policy}
              tabIndex={0}
              className="group relative rounded border border-border/60 p-2.5 cursor-help outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
            >
              <p className="text-[11px] font-medium text-muted-foreground">
                {v.policy}
              </p>
              <p className="text-2xl font-semibold font-mono tabular-nums">
                {v.retire_ready_age == null
                  ? "—"
                  : `age ${v.retire_ready_age.toFixed(0)}`}
              </p>
              {/* Styled hover/focus panel — replaces the native title tooltip. */}
              <div
                role="tooltip"
                className="pointer-events-none absolute left-0 top-full z-30 mt-2 hidden w-[22rem] max-w-[90vw] rounded-lg border border-border/70 bg-popover p-3 text-left text-xs leading-relaxed text-popover-foreground shadow-xl group-hover:block group-focus-visible:block"
              >
                <p className="mb-1 font-semibold text-foreground">{v.policy}</p>
                <p className="mb-2">
                  <span className="font-semibold text-muted-foreground">Why this age: </span>
                  {why}
                </p>
                {means ? (
                  <p>
                    <span className="font-semibold text-muted-foreground">What it means: </span>
                    {means}
                  </p>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
      <p className="text-[11px] text-muted-foreground mt-2">
        Both ages are computed live (typical 5%-real market, deconcentrated +
        reserve-netted, pension + Bituach Leumi credited from 67). Hover or focus
        a tile for the why and the implications.
        {target != null
          ? ` Your plan currently targets age ${target.toFixed(0)} — that's an input you chose, not a safe-age result.`
          : ""}
      </p>

      {/* At-95 estate enrichment. Loading shows a "computing…" hint; an error
          hides the block entirely (the two tiles above stand on their own). */}
      {enrichState === "loading" ? (
        <p className="text-[11px] text-muted-foreground mt-3 italic">
          Computing at-95 estate and assumptions…
        </p>
      ) : enrichState === "ready" && plan ? (
        <>
          <EstateExplorer plan={plan} tileAges={tileAges} />
          <AssumptionsDisclosure plan={plan} />
        </>
      ) : null}
    </div>
  );
}

const REGIME_LABEL: Record<DualTrackTrack["name"], string> = {
  bull: "Bull",
  typical: "Typical",
  bear: "Bear",
};
const REGIME_ORDER: Array<DualTrackTrack["name"]> = ["bull", "typical", "bear"];

// Plain-English description of each market regime — answers "what does the MC
// actually assume, and is 'bear' a 30-year slump?" right in the UI.
function regimeBlurb(name: DualTrackTrack["name"]): string {
  switch (name) {
    case "bull":
      return "a sustained good market (~6% real return per year).";
    case "bear":
      return "a −25% crash right as you retire plus a weak first decade — the sequence-of-returns risk that actually sinks retirements — then a normal market. It is NOT 30 years of falling prices; and every simulated path still has its own full year-to-year ups and downs around that lower start.";
    default:
      return "the central case (~5% real return per year).";
  }
}

// Solvency → text colour: green ≥90%, amber 70–90%, rose <70%.
function solvencyTone(s: number): string {
  if (s >= 0.9) return "text-emerald-600 dark:text-emerald-400";
  if (s >= 0.7) return "text-amber-600 dark:text-amber-400";
  return "text-rose-600 dark:text-rose-400";
}

// Interactive at-95 explorer: pick a retire age (the two computed anchors) and a
// market regime, and the solvency + estate readout updates live. Replaces the
// static per-age tables so the data is clear and self-driven.
function EstateExplorer({
  plan,
  tileAges,
}: {
  plan: DualTrackPlanResponse;
  tileAges: Array<{ label: string; age: number }>;
}) {
  // Distinct anchor ages that actually have frontier data, in tile order.
  const ages = tileAges.filter(
    (t, i, arr) =>
      arr.findIndex((x) => x.age === t.age) === i &&
      estateRowsForAge(plan, t.age).length > 0,
  );
  const [age, setAge] = useState<number | null>(null);
  const [regime, setRegime] = useState<DualTrackTrack["name"]>("typical");
  if (ages.length === 0) return null;
  const activeAge = age ?? ages[0].age;
  const row =
    estateRowsForAge(plan, activeAge).find((r) => r.name === regime) ?? null;
  const nPaths = assumpNum(plan.assumptions ?? {}, "n_paths");

  const segBtn = (active: boolean) =>
    `px-3 py-1 text-xs font-medium font-mono transition-colors ${
      active
        ? "bg-primary text-primary-foreground"
        : "bg-transparent text-muted-foreground hover:bg-muted/50"
    }`;

  return (
    <div className="mt-3 rounded border border-border/50 bg-background/40 p-3">
      <p className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
        Explore — retire at … in a … market
      </p>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 mb-3">
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-muted-foreground">Retire age</span>
          <div className="inline-flex overflow-hidden rounded-md border border-border/60">
            {ages.map((a) => (
              <button
                key={a.age}
                type="button"
                onClick={() => setAge(a.age)}
                className={segBtn(activeAge === a.age)}
              >
                {a.age}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-muted-foreground">Market</span>
          <div className="inline-flex overflow-hidden rounded-md border border-border/60">
            {REGIME_ORDER.map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => setRegime(name)}
                className={segBtn(regime === name)}
              >
                {REGIME_LABEL[name]}
              </button>
            ))}
          </div>
        </div>
      </div>

      {row ? (
        <div className="rounded-md border border-border/60 bg-muted/10 p-3">
          <p className="text-sm">
            Retire at{" "}
            <span className="font-semibold font-mono">{activeAge}</span>, in a{" "}
            <span className="font-semibold">{REGIME_LABEL[regime]}</span> market:
          </p>
          <p
            className={`mt-1 text-2xl font-semibold ${solvencyTone(row.solvent)}`}
          >
            {pct(row.solvent)} chance your money lasts to age 95
          </p>
          <p className="mt-1 text-sm text-muted-foreground">
            At 95 you&apos;d have{" "}
            <span className="font-mono text-foreground">
              {formatNis(row.medianRealNis)}
            </span>{" "}
            on a median path ·{" "}
            <span className="font-mono text-foreground">
              {formatNis(row.worst10RealNis)}
            </span>{" "}
            in the worst 10% — in today&apos;s money.
          </p>
          <p className="mt-2 text-[11px] text-muted-foreground">
            <strong className="text-foreground">{REGIME_LABEL[regime]}</strong> ={" "}
            {regimeBlurb(regime)}
          </p>
        </div>
      ) : (
        <p className="text-[11px] text-muted-foreground">
          No simulation data for this combination.
        </p>
      )}

      <p className="mt-2 text-[11px] text-muted-foreground">
        <strong className="text-foreground">&ldquo;Solvent&rdquo;</strong> = your
        portfolio never runs out before age 95.{" "}
        {nPaths != null ? `Of ~${nPaths.toLocaleString()} ` : "Of the "}simulated
        market paths — each with its own random year-to-year ups and downs, not a
        straight line — this is the share in which the money lasts.{" "}
        <strong className="text-foreground">&ldquo;Estate&rdquo;</strong> is the
        real (inflation-adjusted) wealth still left at 95.
      </p>
    </div>
  );
}

function AssumptionsDisclosure({ plan }: { plan: DualTrackPlanResponse }) {
  const [open, setOpen] = useState(false);
  const a = plan.assumptions ?? {};

  const muTypical = assumpNum(a, "mu_real_typical");
  const muBull = assumpNum(a, "mu_real_bull");
  const muCons = assumpNum(a, "mu_real_conservative");
  const withdrawalTax = assumpNum(a, "withdrawal_tax");
  const inflation = assumpNum(a, "inflation");
  const sigmaCurrent = assumpNum(a, "sigma_current_calibrated");
  const sigmaDiversified = assumpNum(a, "sigma_diversified");
  const reserveDiscount = assumpNum(a, "reserve_discount_real");
  const barDrawdown = assumpNum(a, "bar_drawdown");
  const preservationTest = assumpStr(a, "preservation_test");

  // Nominal ≈ real + inflation, used to spell out "real = after inflation".
  const muNominalApprox =
    muTypical != null && inflation != null ? muTypical + inflation : null;

  return (
    <section className="mt-3 rounded border border-border/40 bg-background/40 p-2.5 text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left flex items-center gap-2"
      >
        <span className="font-mono text-muted-foreground">
          {open ? "▼" : "▸"}
        </span>
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
          Assumptions &amp; how this age is computed
        </span>
      </button>
      {open ? (
        <ul className="mt-2 flex flex-col gap-2 text-muted-foreground">
          <li>
            <strong className="text-foreground">
              There is no single &ldquo;target portfolio.&rdquo;
            </strong>{" "}
            This age is the earliest at which 90% of simulated market paths keep
            you solvent to 95 — solvency-based, not target-based.
          </li>
          <li>
            <strong className="text-foreground">Deployable capital</strong> —{" "}
            {formatNis(plan.deployable_nis)} = {formatNis(plan.full_portfolio_nis)}{" "}
            portfolio − {formatNis(plan.cgt_haircut_nis)} NVDA-sale CGT −{" "}
            {formatNis(plan.reserve_pv_nis)} reserve (PV). This is the money the
            simulation actually invests.
          </li>
          <li>
            <strong className="text-foreground">Expected return</strong> —{" "}
            {pct(muTypical, 1)} REAL (real = after inflation
            {muNominalApprox != null
              ? `, ≈${(muNominalApprox * 100).toFixed(1)}% nominal at ${pct(
                  inflation,
                  1,
                )} inflation`
              : ""}
            ). Bull case {pct(muBull, 1)}, conservative {pct(muCons, 1)} — the
            three regimes you see in the table.
          </li>
          <li>
            <strong className="text-foreground">Volatility</strong> — glides{" "}
            {pct(sigmaCurrent, 0)}→{pct(sigmaDiversified, 0)} as NVDA is sold down
            to its strategic cap, so concentration risk fades as you diversify.
          </li>
          <li>
            <strong className="text-foreground">Withdrawal tax</strong> —{" "}
            {pct(withdrawalTax, 0)} (interim, basis-aware) applied to what you
            draw, not the whole balance.
          </li>
          <li>
            <strong className="text-foreground">Inflation</strong> —{" "}
            {pct(inflation, 1)}; every figure above is in today&apos;s real
            shekels.
          </li>
          <li>
            <strong className="text-foreground">Spend</strong> — central{" "}
            {formatNis(plan.spend_central_nis)} (incl. healthcare) vs stress{" "}
            {formatNis(plan.spend_stress_nis)} (adds home upgrades); the age uses
            the central spend, the stress line is the sensitivity.
          </li>
          <li>
            <strong className="text-foreground">Reserve</strong> — held at PV{" "}
            {formatNis(plan.reserve_pv_nis)} (safe-rate discounted,{" "}
            {pct(reserveDiscount, 1)} real), not the full{" "}
            {formatNis(plan.reserve_raw_nis)} removed upfront — so a finite
            liability doesn&apos;t over-penalize the deployable base.
          </li>
          <li>
            <strong className="text-foreground">Bars</strong> — drawdown ={" "}
            {pct(barDrawdown, 0)} solvent to 95; capital-preservation ={" "}
            {preservationTest ?? "worst-10% real terminal ≥ today's real principal"}
            .
          </li>
        </ul>
      ) : null}
    </section>
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
