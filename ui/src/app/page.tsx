"use client";

import { Anchor } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { AdvisorBriefCard } from "@/components/advisor-brief-card";
import { LiveClock } from "@/components/live-clock";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { SectionHeader } from "@/components/ui/section-header";
import { Sparkline, type SparklineTone } from "@/components/ui/sparkline";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type AgentActivityRow,
  type ArgonautSnapshot,
  type DailyBriefDTO,
  type DomainKbTreeNode,
  type DraftResponse,
  type FleetSelfReviewDTO,
  type InFlightSynthesisDTO,
  type PlanCurrentDTO,
  type PortfolioSnapshotDTO,
} from "@/lib/api";
import Link from "next/link";
import { useWSEvents } from "@/lib/ws";
import { DecisionAccordion } from "@/components/agent/DecisionAccordion";

const USER_ID = "ariel";

// SDD §3.1 fleet size and §5.1 cadence-loop count. Hardcoded today; can later
// be sourced from a /config endpoint.
const AGENT_FLEET_SIZE = 17;
const CADENCE_LOOPS = 9;

// Pass-2 hardcoded knobs (see UI brief). MONTHLY_BUDGET_USD is the
// tip-of-spend cap shown in the SYSTEM tile; NVDA_TARGET_2026 is the YTD
// shares-sold target rendered in the NVDA PACE tile.
const MONTHLY_BUDGET_USD = 200;
const NVDA_TARGET_2026 = 10000;

// Cadence loops shown in the CADENCES TODAY strip, in declared order.
const CADENCE_NAMES = [
  "minute",
  "hour",
  "daily_brief",
  "weekly_review",
  "monthly_cycle",
  "process_cooling",
  "reconcile",
  "audit",
  "watchlist",
] as const;

interface HealthStatus {
  ok: boolean;
  checkedAt: number; // ms epoch
}

interface DbSizeResponse {
  size_bytes?: number;
  size_human?: string;
}

interface AuditEventRow {
  id: number;
  event_type: string;
  created_at: string;
  payload_json: string;
}

interface HomeData {
  portfolio: PortfolioSnapshotDTO | null;
  plan: PlanCurrentDTO | null;
  // Used for the NVDA PACE tile's real-numbers wiring — the draft response
  // carries the latest concentration agent_report's nvda_pace block. Null
  // when no pending draft exists (newly bootstrapped accounts).
  planDraft: DraftResponse | null;
  brief: DailyBriefDTO | null;
  agents: AgentActivityRow[];
  argonautSnapshots: ArgonautSnapshot[];
  health: HealthStatus | null;
  dbSize: string | null;
  monthlySpend: number | null;
  domainKb: DomainKbTreeNode | null;
  cadenceLastTick: Record<string, string | null>;
  // Most-recent fleet self-review report.  Surfaced as a banner so the
  // user sees RED / AMBER counts the moment they hit the page, BEFORE
  // having to ask "is anything broken?".
  fleetReview: FleetSelfReviewDTO | null;
  // Live snapshot of an in-flight plan synthesis run (or null when
  // nothing is running). Surfaced as a banner at the top of the home
  // page so the user can SEE that the fleet is working without having
  // to navigate to /plan first. Polled every 10 s while non-null.
  inFlightSynthesis: InFlightSynthesisDTO | null;
  error: string | null;
}

const initial: HomeData = {
  portfolio: null,
  plan: null,
  planDraft: null,
  brief: null,
  agents: [],
  argonautSnapshots: [],
  health: null,
  dbSize: null,
  monthlySpend: null,
  domainKb: null,
  cadenceLastTick: {},
  fleetReview: null,
  inFlightSynthesis: null,
  error: null,
};

/** Generate a plausible declining curve from `start` to `end` over n points. */
function decliningCurve(start: number, end: number, n: number): number[] {
  if (n <= 1) return [start];
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const t = i / (n - 1);
    // Linear interpolation with a tiny sinusoidal jitter for a less-flat look.
    const base = start + (end - start) * t;
    const jitter = Math.sin(i * 1.7) * (Math.abs(start - end) * 0.04);
    out.push(base + jitter);
  }
  return out;
}

function startOfYearISO(): string {
  const y = new Date().getFullYear();
  return new Date(Date.UTC(y, 0, 1)).toISOString();
}

function startOfMonthISO(): string {
  const now = new Date();
  return new Date(Date.UTC(now.getFullYear(), now.getMonth(), 1)).toISOString();
}

function pctOfYearElapsed(): number {
  const now = new Date();
  const start = new Date(now.getFullYear(), 0, 1).getTime();
  const end = new Date(now.getFullYear() + 1, 0, 1).getTime();
  return ((now.getTime() - start) / (end - start)) * 100;
}

/** Human-readable byte formatter (binary). */
function humanBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export default function Home() {
  const [data, setData] = useState<HomeData>(initial);
  const [loading, setLoading] = useState(true);

  // Per-section flash flags. Each is set to a monotonically increasing
  // counter when its event arrives, so a fresh `.argosy-flash-border`
  // class reliably re-triggers the CSS animation.
  const [proposalFlash, setProposalFlash] = useState(0);
  // activityFlash removed — agent.run.finished no longer drives home-page
  // refresh (see useWSEvents comment below). The accordion's own live
  // updates are the signal; FlashBorderBox receives a static key of 0.

  const refresh = useCallback(async () => {
    try {
      // We try a bunch of endpoints. Each is wrapped so a 404 / network
      // failure on one section doesn't cascade and leave the page blank.
      const [
        portfolio,
        plan,
        planDraft,
        brief,
        agents,
        argonautSnaps,
        healthRes,
        dbSizeRes,
        domainKb,
        monthlySummary,
        monthlyAgentRows,
        cadenceTickAudit,
        fleetReviewLatest,
        inFlightSynth,
      ] = await Promise.all([
        api.portfolioSnapshot(USER_ID).catch(() => null),
        api.planCurrent(USER_ID).catch(() => null),
        // Used by the NVDA PACE tile to read nvda_pace.shares_sold_ytd. We
        // tolerate 404 (no pending draft yet) by falling back to null; the
        // tile then renders an "Awaiting synthesis run" hint.
        api.planDraft(USER_ID).catch(() => null),
        api.dailyBriefLatest(USER_ID).catch(() => null),
        api.agentActivity(USER_ID, 30).catch(() => ({
          rows: [] as AgentActivityRow[],
          next_since: null,
        })),
        api
          .argonautSnapshots(USER_ID, 90)
          .catch(() => ({ rows: [] as ArgonautSnapshot[] })),
        // Health probe — relative URL; rewrites in next.config send /api/* to
        // the backend at :8000/api/*. Pass-2 expects a JSON-ish 200 OK.
        fetch("/api/health", { cache: "no-store" })
          .then((r) => ({ ok: r.ok, checkedAt: Date.now() }) as HealthStatus)
          .catch(() => null),
        // Optional internal endpoint — we don't expect this to exist yet.
        fetch("/api/internal/db-size", { cache: "no-store" })
          .then(async (r): Promise<string | null> => {
            if (!r.ok) return null;
            const j = (await r.json()) as DbSizeResponse;
            if (typeof j.size_human === "string") return j.size_human;
            if (typeof j.size_bytes === "number")
              return humanBytes(j.size_bytes);
            return null;
          })
          .catch(() => null),
        api.domainKbTree().catch(() => null),
        // Monthly cost summary — try the audit-event slot first.
        api
          .auditList(USER_ID, {
            eventType: "cost.monthly_summary",
            limit: 1,
          })
          .catch(() => null),
        // Fallback: sum cost_usd from agent_activity rows in the current
        // month. We re-fetch a wider window for this.  detail=false drops
        // response_text / citations_json / sources_preview to keep the
        // payload small (~KB vs multi-MB for a busy account).
        api
          .agentActivity(USER_ID, 500, { detail: false })
          .catch(() => ({
            rows: [] as AgentActivityRow[],
            next_since: null,
          })),
        api
          .auditList(USER_ID, {
            eventType: "cadence.tick",
            since: startOfYearISO(),
            limit: 200,
          })
          .catch(() => null),
        // Fleet self-review banner — most-recent report.  Fails gracefully
        // when the migration hasn't been applied yet or no report exists.
        api.fleetSelfReviewLatest(USER_ID).catch(() => null),
        // In-flight synthesis banner — backend returns 200+null when
        // nothing is running, so a swallowed network/404 just yields the
        // same shape.  Polled every 10 s by the effect below while
        // non-null so the phase counter ticks up live.
        api
          .planInFlightSynthesis(USER_ID)
          .catch(() => ({ in_flight_synthesis: null })),
      ]);

      // ---- Monthly spend resolution -------------------------------------
      let monthlySpend: number | null = null;
      const summaryRow = monthlySummary?.rows?.[0] as
        | AuditEventRow
        | undefined;
      if (summaryRow) {
        try {
          const parsed = JSON.parse(summaryRow.payload_json) as {
            total_usd?: number;
          };
          if (typeof parsed.total_usd === "number")
            monthlySpend = parsed.total_usd;
        } catch {
          /* ignore parse errors; fall through */
        }
      }
      if (monthlySpend === null) {
        const monthStart = new Date(startOfMonthISO()).getTime();
        let sum = 0;
        for (const r of monthlyAgentRows.rows) {
          const t = new Date(r.created_at).getTime();
          if (t >= monthStart) sum += r.cost_usd;
        }
        monthlySpend = sum;
      }

      // ---- Cadence tick resolution --------------------------------------
      const cadenceLastTick: Record<string, string | null> = {};
      for (const name of CADENCE_NAMES) cadenceLastTick[name] = null;
      const tickRows = (cadenceTickAudit?.rows ?? []) as AuditEventRow[];
      for (const row of tickRows) {
        try {
          const parsed = JSON.parse(row.payload_json) as { loop?: string };
          const loop = parsed.loop;
          if (
            typeof loop === "string" &&
            (CADENCE_NAMES as readonly string[]).includes(loop) &&
            !cadenceLastTick[loop]
          ) {
            cadenceLastTick[loop] = row.created_at;
          }
        } catch {
          /* ignore malformed payload */
        }
      }

      setData({
        portfolio,
        plan,
        planDraft,
        brief,
        agents: agents?.rows ?? [],
        argonautSnapshots: argonautSnaps?.rows ?? [],
        health: healthRes,
        dbSize: dbSizeRes,
        monthlySpend,
        domainKb,
        cadenceLastTick,
        fleetReview: fleetReviewLatest,
        inFlightSynthesis: inFlightSynth?.in_flight_synthesis ?? null,
        error: null,
      });
    } catch (e: unknown) {
      setData((prev) => ({ ...prev, error: String(e) }));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Refresh on relevant WS events; also fire per-section "flash"
  // animations so users get a real-time signal a section just changed.
  //
  // NOTE: agent.run.finished is intentionally excluded here. A cascade run
  // can emit ~20 of these events per advisor turn; including it caused 20
  // full home-page refreshes per turn. The DecisionAccordion already handles
  // agent.run.finished updates via useDecisionStream. activityFlash is dropped
  // as redundant with the accordion's live updates.
  const lastEvent = useWSEvents([
    "daily_brief.ready",
    "proposal.created",
    "proposal.updated",
    // Self-review fires on every synthesis completion; banner needs to
    // refresh so the user sees the new RED / AMBER counts without a
    // manual page reload.
    "fleet_self_review.completed",
  ]);
  useEffect(() => {
    if (!lastEvent) return;
    if (
      lastEvent.event === "proposal.created" ||
      lastEvent.event === "proposal.updated"
    ) {
      setProposalFlash((n) => n + 1);
    }
    refresh();
  }, [lastEvent, refresh]);

  // Poll the in-flight synthesis endpoint while one is running so the
  // phase counter on the "Synthesis #N in flight" banner ticks up live.
  // The backend doesn't emit per-phase WS events, so without polling
  // the banner would freeze at "phase 0 of 5" until plan.draft.completed
  // arrived ~30 min later.  10 s cadence matches /plan; the route is
  // cheap (indexed DecisionRun lookup + one DecisionPhase count).  The
  // interval clears whenever inFlightSynthesis flips back to null
  // (synth completed or was never running on the most recent refresh).
  useEffect(() => {
    if (data.inFlightSynthesis == null) return;
    const handle = window.setInterval(() => {
      api
        .planInFlightSynthesis(USER_ID)
        .then((r) =>
          setData((prev) => ({
            ...prev,
            inFlightSynthesis: r.in_flight_synthesis ?? null,
          })),
        )
        .catch(() => {
          // Swallow transient errors; the next tick (or the next
          // refresh()) will recover.  A polling hiccup shouldn't make
          // the banner disappear.
        });
    }, 10_000);
    return () => window.clearInterval(handle);
  }, [data.inFlightSynthesis]);

  // ----- Derived values --------------------------------------------------
  const netWorth = data.portfolio?.total_usd_value_k ?? 0;
  const planSummary = (data.plan?.latest_critique_json as
    | { overall_summary?: string; findings?: { severity?: string }[] }
    | null
    | undefined) || null;
  const findings = planSummary?.findings ?? [];
  const planStatus =
    findings.find((f) => f.severity === "RED")
      ? { tone: "error" as const, label: "RED" }
      : findings.find((f) => f.severity === "YELLOW")
        ? { tone: "warning" as const, label: "YELLOW" }
        : { tone: "success" as const, label: "GREEN" };

  // Concentration scorecard: pull NVDA % from positions.
  const totalUsdK = netWorth;
  const nvdaPos = data.portfolio?.positions.find((p) => p.symbol === "NVDA");
  const nvdaPct =
    totalUsdK > 0 && nvdaPos?.usd_value_k
      ? (nvdaPos.usd_value_k / totalUsdK) * 100
      : null;
  const concentrationTone: "success" | "warning" | "error" =
    nvdaPct === null
      ? "warning"
      : nvdaPct > 30
        ? "error"
        : nvdaPct > 15
          ? "warning"
          : "success";

  // Sparkline series (12 points each).
  const netWorthSeries = useMemo(() => {
    const snaps = data.argonautSnapshots;
    if (snaps.length >= 2) {
      // Snapshots come newest-first by convention; reverse to chronological.
      return [...snaps]
        .reverse()
        .slice(-12)
        .map((s) => s.total_value_usd);
    }
    return Array(12).fill(0);
  }, [data.argonautSnapshots]);

  const concentrationSeries = useMemo(() => {
    if (nvdaPct === null) return Array(12).fill(0);
    // Generate a plausible declining curve from current → 15% target.
    return decliningCurve(nvdaPct, 15, 12);
  }, [nvdaPct]);

  const proposalsSeries = useMemo(() => Array(12).fill(0), []);

  // Argonaut P&L since inception (reverse-chronologically corrected).
  const argonautSeries = useMemo(() => {
    const snaps = data.argonautSnapshots;
    if (snaps.length === 0) return [];
    return [...snaps].reverse().map((s) => s.total_value_usd);
  }, [data.argonautSnapshots]);

  const argonautDayDelta = useMemo(() => {
    const snaps = data.argonautSnapshots;
    if (snaps.length === 0) return null;
    return snaps[0].day_pnl_usd;
  }, [data.argonautSnapshots]);

  // System tile values.
  const engineActive = !!(
    data.health?.ok &&
    Date.now() - (data.health?.checkedAt ?? 0) < 60_000
  );
  const killSwitchArmed =
    process.env.NEXT_PUBLIC_ARGOSY_KILL === undefined ||
    process.env.NEXT_PUBLIC_ARGOSY_KILL === "armed" ||
    process.env.NEXT_PUBLIC_ARGOSY_KILL === "ARMED";

  // NVDA pace. Sourced from the latest concentration agent_report tied to
  // the user's pending draft (see backend ``_build_nvda_pace`` in
  // argosy/api/routes/plan.py). Falls back to 0 + an "awaiting synthesis"
  // tooltip when no concentration report exists yet — the tile still
  // renders so the user sees the target rather than a blank slot.
  const nvdaPace = data.planDraft?.nvda_pace ?? null;
  const nvdaSold = nvdaPace?.shares_sold_ytd ?? 0;
  const nvdaPctSold = (nvdaSold / NVDA_TARGET_2026) * 100;
  // Prefer the agent's explicit ``on_track`` boolean when we have one; it's
  // computed against the YTD pro-rated target (target_shares_ytd), which is
  // a tighter signal than "pct of annual target vs pct of year elapsed".
  // When nvda_pace is unavailable, fall back to the prior heuristic so the
  // tile still toggles between ON PACE / BEHIND PACE.
  const nvdaOnPace =
    nvdaPace !== null
      ? nvdaPace.on_track
      : nvdaPctSold >= pctOfYearElapsed();
  const nvdaPaceTooltip =
    nvdaPace === null ? "Awaiting synthesis run" : undefined;

  // Domain KB freshness.
  const kbStats = useMemo(() => {
    if (!data.domainKb) return null;
    const sixMonthsMs = 6 * 30 * 24 * 60 * 60 * 1000;
    const cutoff = Date.now() - sixMonthsMs;
    let total = 0;
    let fresh = 0;
    let dueSoon = 0;
    let stale = 0;
    const walk = (n: DomainKbTreeNode) => {
      if (!n.is_dir) {
        total += 1;
        // We don't have last_verified_at on tree nodes; treat all as fresh
        // for now (the file endpoint exposes frontmatter, but walking every
        // file would be N+1). The cutoff comparison is preserved so when a
        // server-side aggregate lands, this code falls into the right bucket
        // without changing the UI.
        const verifiedAt = (n as DomainKbTreeNode & {
          last_verified_at?: string;
        }).last_verified_at;
        if (typeof verifiedAt === "string") {
          const t = new Date(verifiedAt).getTime();
          if (t < cutoff) stale += 1;
          else if (t < cutoff + sixMonthsMs / 4) dueSoon += 1;
          else fresh += 1;
        } else {
          fresh += 1;
        }
      }
      for (const child of n.children ?? []) walk(child);
    };
    walk(data.domainKb);
    return { total, fresh, dueSoon, stale };
  }, [data.domainKb]);

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      {/* Brand hero card. Pared-down treatment after the Plan/Codex
          ideation pass: the prior version stacked three gradient
          flourishes (.argosy-hero-ring + top-edge stripe + the body
          radial glow) in 500px of viewport, which both engines
          flagged as competing for attention. Kept ONE atmospheric
          (the body radial), replaced the rest with a single
          left-edge accent rule in the brand-green semantic token.
          The 🚢 emoji also got swapped for a Lucide Anchor icon in a
          tinted CardIcon-style square — same nautical metaphor,
          finance-serious typography. */}
      <section
        className="relative rounded-xl overflow-hidden bg-card/80 backdrop-blur-sm border border-border border-l-2 border-l-success/60"
        data-slot="brand-hero"
      >
        <div className="relative px-6 py-5 flex items-start justify-between gap-4 flex-wrap">
          <div className="flex items-start gap-4 min-w-0">
            <span
              className="inline-flex h-12 w-12 shrink-0 items-center justify-center rounded-md border border-success/30 bg-success/10 text-success"
              aria-hidden
              suppressHydrationWarning
            >
              <Anchor className="h-5 w-5" />
            </span>
            <div className="min-w-0">
              <h1 className="font-mono font-bold text-2xl leading-tight">
                <span className="text-foreground">Welcome to </span>
                <span className="text-success">Argosy</span>
              </h1>
              <p className="text-sm text-muted-foreground mt-1">
                multi-agent financial advisor — paper-mode by default,
                audit-trail by design
              </p>
              <div className="flex items-center gap-2 mt-3 flex-wrap">
                <StatusPill tone="neutral" mono>
                  v0.1.0
                </StatusPill>
                <StatusPill tone="neutral" mono>
                  {AGENT_FLEET_SIZE} agents
                </StatusPill>
                <StatusPill tone="neutral" mono>
                  {CADENCE_LOOPS} cadence loops
                </StatusPill>
                <StatusPill tone="accent" mono>
                  paper mode
                </StatusPill>
              </div>
            </div>
          </div>
          <div className="shrink-0">
            <LiveClock className="text-base" />
          </div>
        </div>
      </section>

      {/* In-flight synthesis banner — surfaces a "Synthesis #N in
          flight" card at the top of home so the user can SEE that
          the fleet is actively working without first having to
          navigate to /plan.  Suppressed when nothing is running.
          The polling effect above ticks the phase counter up live
          every 10 s while non-null. */}
      {data.inFlightSynthesis ? (
        <InFlightSynthesisBanner inFlight={data.inFlightSynthesis} />
      ) : null}

      {/* Fleet self-review banner — auto-fires after every synthesis +
          daily.  Surfaces RED/AMBER counts so the user can SEE
          anomalies without asking "did anything go wrong?".  Hidden
          when no report exists yet (fresh install). */}
      {data.fleetReview ? (
        <FleetSelfReviewBanner report={data.fleetReview} />
      ) : null}

      {/* Advisor brief — front-and-center glance card so the advisor is a
          primary surface, not buried in nav. Composed server-side from
          gap-tracker + daily-brief + watchlist signals (no LLM call). */}
      <AdvisorBriefCard userId={USER_ID} />

      {/* Compact metric row — now with sparklines. */}
      <section>
        <SectionHeader label="OVERVIEW" />
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <MetricTile
            label="Net worth"
            value={loading ? "…" : `$${netWorth.toLocaleString()}K`}
            pillLabel="liquid"
            pillTone="neutral"
            sub="Δ vs prior — wired in Phase 4"
            sparkData={netWorthSeries}
            sparkTone="success"
          />
          <MetricTile
            label="Concentration"
            value={nvdaPct === null ? "—" : `${nvdaPct.toFixed(1)}%`}
            pillLabel="NVDA"
            pillTone={concentrationTone}
            sub="Sector caps wire in Phase 3"
            sparkData={concentrationSeries}
            sparkTone="accent"
          />
          <MetricTile
            label="Pending proposals"
            value="0"
            pillLabel="idle"
            pillTone="neutral"
            sub="Proposals queue arrives in Phase 3"
            sparkData={proposalsSeries}
            sparkTone="neutral"
          />
        </div>
      </section>

      {/* ARGONAUT card — chart only renders when we have ≥2 snapshots. */}
      <section>
        <SectionHeader
          label="ARGONAUT"
          action={
            argonautDayDelta !== null ? (
              <StatusPill
                tone={argonautDayDelta >= 0 ? "success" : "error"}
                mono
              >
                Δ ${argonautDayDelta.toFixed(2)}
              </StatusPill>
            ) : (
              <StatusPill tone="neutral" mono>
                no data
              </StatusPill>
            )
          }
        />
        <div className="rounded-lg border border-border bg-card px-4 py-3">
          {argonautSeries.length >= 2 ? (
            <Sparkline
              data={argonautSeries}
              height={72}
              tone={
                (argonautDayDelta ?? 0) >= 0 ? "success" : "error"
              }
              ariaLabel="Argonaut P&L since inception"
            />
          ) : (
            <div className="h-[72px] flex items-center justify-center text-xs text-muted-foreground font-mono">
              no positions yet · awaiting first paper trade
            </div>
          )}
        </div>
      </section>

      {/* SYSTEM tile row */}
      <section>
        <SectionHeader label="SYSTEM" />
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <SystemTile
            label="Engine"
            value={engineActive ? "ACTIVE" : "DOWN"}
            tone={engineActive ? "success" : "error"}
            pulse={engineActive}
          />
          <SystemTile
            label="Kill switch"
            value={killSwitchArmed ? "ARMED" : "DISARMED"}
            tone={killSwitchArmed ? "success" : "warning"}
          />
          <div className="rounded-lg border border-border bg-card px-3 py-2.5 flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
                Monthly spend
              </span>
              <StatusPill
                tone={
                  data.monthlySpend !== null &&
                  data.monthlySpend > MONTHLY_BUDGET_USD
                    ? "error"
                    : "neutral"
                }
                mono
              >
                cap ${MONTHLY_BUDGET_USD}
              </StatusPill>
            </div>
            <div className="font-mono text-base font-semibold tabular-nums">
              {data.monthlySpend === null
                ? "—"
                : `$${data.monthlySpend.toFixed(2)}`}
            </div>
            <ProgressBar
              pct={
                data.monthlySpend === null
                  ? 0
                  : Math.min(
                      100,
                      (data.monthlySpend / MONTHLY_BUDGET_USD) * 100,
                    )
              }
              tone={
                data.monthlySpend !== null &&
                data.monthlySpend > MONTHLY_BUDGET_USD
                  ? "error"
                  : "accent"
              }
            />
          </div>
          <SystemTile label="DB size" value={data.dbSize ?? "—"} tone="neutral" />
        </div>
      </section>

      {/* CADENCES TODAY strip */}
      <section>
        <SectionHeader label="CADENCES TODAY" count={CADENCE_NAMES.length} />
        <div className="rounded-lg border border-border bg-card px-3 py-2.5 flex flex-wrap gap-2">
          {CADENCE_NAMES.map((name) => {
            const last = data.cadenceLastTick[name];
            const lastT = last ? new Date(last).getTime() : 0;
            const ageMin = last ? (Date.now() - lastT) / 60_000 : null;
            const dotClass =
              ageMin === null
                ? "bg-muted-foreground/40"
                : ageMin < 30
                  ? "bg-success"
                  : ageMin < 240
                    ? "bg-warning"
                    : "bg-muted-foreground/50";
            const lastLabel =
              last === null
                ? "—"
                : new Date(last).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                  });
            return (
              <span
                key={name}
                className="inline-flex items-center gap-2 rounded-full border border-border bg-secondary/40 px-2.5 py-1"
              >
                <span
                  aria-hidden
                  className={`inline-block h-1.5 w-1.5 rounded-full ${dotClass}`}
                />
                <span className="font-mono text-[11px]">{name}</span>
                <span className="font-mono text-[11px] text-muted-foreground tabular-nums">
                  {lastLabel}
                </span>
              </span>
            );
          })}
        </div>
      </section>

      {/* NVDA PACE tile */}
      <section>
        <SectionHeader
          label="NVDA PACE"
          action={
            <StatusPill tone={nvdaOnPace ? "success" : "warning"} mono>
              {nvdaOnPace ? "ON PACE" : "BEHIND PACE"}
            </StatusPill>
          }
        />
        <div
          className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-2"
          title={nvdaPaceTooltip}
        >
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <div className="font-mono text-sm tabular-nums">
              {nvdaSold.toLocaleString()} / {NVDA_TARGET_2026.toLocaleString()}{" "}
              shares sold YTD
            </div>
            <div className="text-[11px] text-muted-foreground tabular-nums">
              {nvdaPctSold.toFixed(1)}% of target ·{" "}
              {pctOfYearElapsed().toFixed(0)}% of year elapsed
              {nvdaPaceTooltip ? ` · ${nvdaPaceTooltip}` : ""}
            </div>
          </div>
          <ProgressBar
            pct={Math.max(0, Math.min(100, nvdaPctSold))}
            tone={nvdaOnPace ? "success" : "warning"}
          />
        </div>
      </section>

      {/* DOMAIN KB FRESHNESS tile */}
      <section>
        <SectionHeader label="DOMAIN KB FRESHNESS" />
        <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-2">
          {kbStats === null ? (
            <div className="text-xs text-muted-foreground font-mono">
              KB tree not yet available · run `argosy kb sync`
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="font-mono text-sm tabular-nums">
                  {kbStats.fresh}/{kbStats.total} files fresh
                </div>
                <div className="flex items-center gap-1.5">
                  <StatusPill tone="success" mono>
                    FRESH {kbStats.fresh}
                  </StatusPill>
                  <StatusPill tone="warning" mono>
                    DUE SOON {kbStats.dueSoon}
                  </StatusPill>
                  <StatusPill tone="error" mono>
                    STALE {kbStats.stale}
                  </StatusPill>
                </div>
              </div>
              <ProgressBar
                pct={
                  kbStats.total === 0
                    ? 0
                    : (kbStats.fresh / kbStats.total) * 100
                }
                tone="success"
              />
            </>
          )}
        </div>
      </section>

      {/* T4.5 — Daily brief lands at the top of the page so the user
          sees it first thing in the morning. When the T4.5 runner has
          produced a brief, render its content_md; otherwise render a
          placeholder explaining when the next brief will land. */}
      <section>
        <SectionHeader
          label="TODAY'S BRIEF"
          action={
            data.brief?.brief_date ? (
              <a
                href="/briefs"
                className="text-[11px] font-mono text-muted-foreground hover:text-foreground underline-offset-2 hover:underline"
              >
                view all
              </a>
            ) : null
          }
        />
        <Card>
          <CardHeader>
            <CardTitle className="font-mono">
              {data.brief?.brief_date
                ? `Brief — ${data.brief.brief_date}`
                : "Daily brief will land tomorrow at 07:00"}
            </CardTitle>
            <CardDescription>
              {data.brief?.run_at
                ? `Generated ${new Date(data.brief.run_at).toLocaleString()}`
                : "Set ARGOSY_DAILY_BRIEF_ENABLED=1 to enable the production scheduler, or run `argosy brief --user-id ariel` for a one-shot."}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {data.brief?.content_md ? (
              <pre className="whitespace-pre-wrap text-xs font-mono text-foreground tabular-nums">
                {data.brief.content_md}
              </pre>
            ) : data.brief?.summary_text ? (
              <pre className="whitespace-pre-wrap text-xs font-mono text-muted-foreground tabular-nums">
                {data.brief.summary_text}
              </pre>
            ) : (
              <p className="text-xs font-mono text-muted-foreground">
                No brief on file yet. The runner fires daily at 07:00
                Asia/Jerusalem when enabled.
              </p>
            )}
          </CardContent>
        </Card>
      </section>

      {/* Plan + brief row */}
      <section>
        <SectionHeader label="PLAN" count={1} />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Card>
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle className="font-mono">Plan adherence</CardTitle>
                <StatusPill tone={planStatus.tone} mono>
                  {planStatus.label}
                </StatusPill>
              </div>
              <CardDescription>
                {data.plan?.version_label
                  ? `Latest: ${data.plan.version_label}`
                  : "No plan imported yet."}
              </CardDescription>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              {planSummary?.overall_summary ||
                "Run `argosy ingest plan <path>` then `argosy critique`."}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="font-mono">Phase 2 brief summary</CardTitle>
              <CardDescription>
                {data.brief?.run_at
                  ? `Generated ${new Date(data.brief.run_at).toLocaleString()}`
                  : "No legacy four-agent brief yet."}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="whitespace-pre-wrap text-xs font-mono text-muted-foreground tabular-nums">
                {data.brief?.summary_text || "(no Phase 2 brief on file)"}
              </pre>
            </CardContent>
          </Card>
        </div>
      </section>

      {/* Proposals — flashes border on proposal.created/updated WS events. */}
      <section>
        <SectionHeader label="PROPOSALS" count={0} />
        <FlashBorderBox flashKey={proposalFlash}>
          <div className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-xs text-muted-foreground font-mono">
            No proposals queued · awaiting Phase 3
          </div>
        </FlashBorderBox>
      </section>

      {/* ACTIVITY — decision-grouped accordion with live WS cascade. */}
      <section>
        <SectionHeader label="ACTIVITY" />
        <FlashBorderBox flashKey={0}>
          <DecisionAccordion userId={USER_ID} />
        </FlashBorderBox>
      </section>

      {data.error && (
        <p className="text-sm text-error font-mono">{data.error}</p>
      )}
    </main>
  );
}

// ---------- Local presentational helpers --------------------------------

interface MetricTileProps {
  label: string;
  value: string;
  pillLabel: string;
  pillTone: "success" | "warning" | "error" | "neutral" | "accent";
  sub: string;
  sparkData: number[];
  sparkTone: SparklineTone;
}

function MetricTile({
  label,
  value,
  pillLabel,
  pillTone,
  sub,
  sparkData,
  sparkTone,
}: MetricTileProps) {
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
          {label}
        </span>
        <StatusPill tone={pillTone} mono>
          {pillLabel}
        </StatusPill>
      </div>
      <div className="font-mono text-xl font-semibold tabular-nums">
        {value}
      </div>
      <Sparkline data={sparkData} tone={sparkTone} height={32} />
      <div className="text-[11px] text-muted-foreground tabular-nums">
        {sub}
      </div>
    </div>
  );
}

interface SystemTileProps {
  label: string;
  value: string;
  tone: "success" | "warning" | "error" | "neutral";
  pulse?: boolean;
}

function SystemTile({ label, value, tone, pulse }: SystemTileProps) {
  const dotClass =
    tone === "success"
      ? "bg-success"
      : tone === "warning"
        ? "bg-warning"
        : tone === "error"
          ? "bg-error"
          : "bg-muted-foreground/50";
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2.5 flex flex-col gap-1.5">
      <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <div className="flex items-center gap-2">
        <span
          aria-hidden
          className={`inline-block h-2 w-2 rounded-full ${dotClass} ${
            pulse ? "argosy-pulse-dot" : ""
          }`}
        />
        <span className="font-mono text-base font-semibold tabular-nums">
          {value}
        </span>
      </div>
    </div>
  );
}

interface ProgressBarProps {
  pct: number;
  tone: "success" | "warning" | "error" | "accent";
}

function ProgressBar({ pct, tone }: ProgressBarProps) {
  const fillClass =
    tone === "success"
      ? "bg-success"
      : tone === "warning"
        ? "bg-warning"
        : tone === "error"
          ? "bg-error"
          : "bg-info";
  return (
    <div className="h-1 w-full rounded-full bg-secondary/60 overflow-hidden">
      <div
        className={`h-full ${fillClass} transition-all duration-500`}
        style={{ width: `${Math.max(0, Math.min(100, pct))}%` }}
      />
    </div>
  );
}

interface FlashBorderBoxProps {
  flashKey: number;
  children: React.ReactNode;
}

function FlashBorderBox({ flashKey, children }: FlashBorderBoxProps) {
  // The `key` prop forces React to remount the wrapper whenever `flashKey`
  // changes, which is what re-fires the CSS animation (CSS animations don't
  // restart on a no-op class change).
  return (
    <div
      key={flashKey}
      className={
        flashKey > 0
          ? "rounded-lg border-t-2 border-t-emerald-400/60 argosy-flash-border"
          : ""
      }
    >
      {children}
    </div>
  );
}

// ----------------------------------------------------------------------
// In-flight synthesis banner — "Synthesis #N in flight · phase X of 5".
// Sits at the very top of the home page (between brand-hero and the fleet
// self-review banner) so the user lands on / and SEES the fleet is
// actively working, instead of having to navigate to /plan to find out.
// Only renders while a plan-revision DecisionRun is running for the user;
// the polling loop in <Home/> refreshes the phase counter every 10 s.
// ----------------------------------------------------------------------

interface InFlightSynthesisBannerProps {
  inFlight: InFlightSynthesisDTO;
}

function InFlightSynthesisBanner({ inFlight }: InFlightSynthesisBannerProps) {
  // Format started_at as HH:MM in the user's locale so "started 18:51"
  // matches the wall clock they're staring at.
  let startedAtLabel = "";
  if (inFlight.started_at) {
    const d = new Date(inFlight.started_at);
    if (!Number.isNaN(d.getTime())) {
      startedAtLabel = d.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
    }
  }

  return (
    <section
      className="rounded-lg border border-border border-l-2 border-l-info/70 bg-card px-4 py-3 flex items-center justify-between gap-3 flex-wrap"
      data-slot="in-flight-synthesis-banner"
    >
      <div className="flex flex-col gap-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span aria-hidden className="font-mono text-sm text-info">
            ⏳
          </span>
          <span className="font-mono text-sm font-semibold">
            Synthesis #{inFlight.decision_run_id} in flight
          </span>
          <StatusPill tone="accent" mono>
            phase {inFlight.completed_phases} of {inFlight.total_phases}
          </StatusPill>
        </div>
        <div className="font-mono text-[11px] text-muted-foreground tabular-nums">
          {startedAtLabel ? `started ${startedAtLabel} · ` : ""}
          status {inFlight.status} · a new draft will appear when complete
          (~30 min)
        </div>
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        <Link
          href={`/decisions/${inFlight.decision_run_id}`}
          className="font-mono text-xs text-info hover:underline"
        >
          Drill in -&gt;
        </Link>
        <Link
          href="/plan"
          className="font-mono text-xs text-info hover:underline"
        >
          View plan -&gt;
        </Link>
      </div>
    </section>
  );
}

// ----------------------------------------------------------------------
// Fleet self-review banner — RED / AMBER / YELLOW glance + "Read report".
// Sits between the brand hero and the advisor brief so anomalies surface
// BEFORE the user starts reading anything else.  The tile only renders
// when a report row exists (api returns null for a fresh install).
// ----------------------------------------------------------------------

interface FleetSelfReviewBannerProps {
  report: FleetSelfReviewDTO;
}

function FleetSelfReviewBanner({ report }: FleetSelfReviewBannerProps) {
  const sev = report.severity_summary;
  const red = sev.RED ?? 0;
  const amber = sev.AMBER ?? 0;
  const yellow = sev.YELLOW ?? 0;
  const total = red + amber + yellow;

  const tone: "success" | "warning" | "error" =
    red > 0 ? "error" : amber > 0 ? "warning" : "success";
  const borderClass =
    tone === "error"
      ? "border-l-error/70"
      : tone === "warning"
        ? "border-l-warning/70"
        : "border-l-success/70";

  const generatedLabel = report.generated_at
    ? new Date(report.generated_at).toLocaleString()
    : "—";

  return (
    <section
      className={`rounded-lg border border-border ${borderClass} border-l-2 bg-card px-4 py-3 flex items-center justify-between gap-3 flex-wrap`}
      data-slot="fleet-self-review-banner"
    >
      <div className="flex flex-col gap-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-mono text-xs uppercase tracking-wider text-muted-foreground">
            Fleet self-review
          </span>
          <span className="font-mono text-[10px] text-muted-foreground/80">
            #{report.id} · {report.scope_kind}
          </span>
        </div>
        <div className="font-mono text-sm">
          {total === 0
            ? "No anomalies detected in scope."
            : `${total} finding${total === 1 ? "" : "s"} — ${red} RED · ${amber} AMBER · ${yellow} YELLOW`}
        </div>
        <div className="font-mono text-[11px] text-muted-foreground">
          generated {generatedLabel}
        </div>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <StatusPill tone="error" mono>
          RED {red}
        </StatusPill>
        <StatusPill tone="warning" mono>
          AMBER {amber}
        </StatusPill>
        <StatusPill tone="neutral" mono>
          YELLOW {yellow}
        </StatusPill>
        <Link
          href={`/fleet-review/${report.id}`}
          className="ml-2 font-mono text-xs text-info hover:underline"
        >
          Read report -&gt;
        </Link>
      </div>
    </section>
  );
}


