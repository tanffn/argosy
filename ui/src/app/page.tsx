"use client";

import { Shield, ShieldOff } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { AdvisorBriefCard } from "@/components/advisor-brief-card";
import { ActionItemsWidget } from "@/components/home/action-items-widget";
import { DeconcentrationCard } from "@/components/home/DeconcentrationCard";
import { FMGreetingCard } from "@/components/home/FMGreetingCard";
import { PlanAdherenceCard } from "@/components/home/PlanAdherenceCard";
import { RedFlagStrip } from "@/components/home/RedFlagStrip";
import { WealthTrajectoryCard } from "@/components/home/WealthTrajectoryCard";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { SectionHeader } from "@/components/ui/section-header";
import { Sparkline } from "@/components/ui/sparkline";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type AgentActivityRow,
  type AnomalyReportDTO,
  type ArgonautSnapshot,
  type DailyBriefDTO,
  type DomainKbTreeNode,
  type DraftResponse,
  type FleetSelfReviewDTO,
  type GreetingDTO,
  type InFlightSynthesisDTO,
  type PlanCurrentDTO,
} from "@/lib/api";
import Link from "next/link";
import { useWSEvents } from "@/lib/ws";
import { DecisionAccordion } from "@/components/agent/DecisionAccordion";

const USER_ID = "ariel";

// SDD §3.1 fleet size and §5.1 cadence-loop count. The live count comes from
// the backend GET /api/config (derived from the public `class \w+Agent`
// declarations under `argosy/agents/`, excluding the abstract `BaseAgent` base
// and the private `_ResearcherAgent` helper). This literal is only the fallback
// shown if that fetch fails, so the hero still renders a sensible number.
const AGENT_FLEET_SIZE_FALLBACK = 51;
// 9 cadence ticks displayed in the CADENCES TODAY strip below (see
// CADENCE_NAMES) — a curated home-page subset of the ~23 loops registered in
// the scheduler, NOT the total registered-loop count.
const CADENCE_LOOPS = 9;

// Pass-2 hardcoded knob (see UI brief). MONTHLY_BUDGET_USD is the
// tip-of-spend cap shown in the SYSTEM tile. The NVDA PACE tile's
// YTD shares-sold target is plan-derived (nvda_pace.target_shares_ytd),
// NOT a hardcoded constant.
const MONTHLY_BUDGET_USD = 200;

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
  // Backend (argosy.api.routes.health.db_size) returns size_bytes +
  // size_human. We tolerate the legacy field names so older backends
  // that haven't been redeployed still render something useful.
  size_bytes?: number;
  size_human?: string;
  bytes?: number;
  human?: string;
}

interface AuditEventRow {
  id: number;
  event_type: string;
  created_at: string;
  payload_json: string;
}

interface HomeData {
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
  // EX2 — most-recent anomaly-detection report. Banner renders ABOVE
  // the fleet-self-review banner so RED anomalies (e.g. Card 2923's
  // fee-waiver promo disappearing) surface FIRST.
  anomalyReport: AnomalyReportDTO | null;
  // Live snapshot of an in-flight plan synthesis run (or null when
  // nothing is running). Surfaced as a banner at the top of the home
  // page so the user can SEE that the fleet is working without having
  // to navigate to /plan first. Polled every 10 s while non-null.
  inFlightSynthesis: InFlightSynthesisDTO | null;
  // Agent fleet size from the backend (GET /api/config). Null until the
  // fetch resolves; the hero falls back to AGENT_FLEET_SIZE_FALLBACK.
  fleetCount: number | null;
  error: string | null;
}

const initial: HomeData = {
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
  anomalyReport: null,
  inFlightSynthesis: null,
  fleetCount: null,
  error: null,
};

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

  // Per-section flash flags. Each is set to a monotonically increasing
  // counter when its event arrives, so a fresh `.argosy-flash-border`
  // class reliably re-triggers the CSS animation.
  const [proposalFlash, setProposalFlash] = useState(0);

  // "Full detail" region — the demoted plumbing (red-flag strip, run
  // banners, legacy cards, system health). Collapsed by default; the
  // greeting card's [Full detail →] button opens + scrolls to it.
  const [fullDetailOpen, setFullDetailOpen] = useState(false);
  // The canonical greeting payload, shared by FMGreetingCard via
  // onLoaded so the Plan-adherence panel reuses the server-side
  // on_plan computation without a second /api/home/greeting fetch.
  const [greeting, setGreeting] = useState<GreetingDTO | null>(null);
  const showFullDetail = useCallback(() => {
    setFullDetailOpen(true);
    // Scroll after the region mounts.
    window.requestAnimationFrame(() => {
      document
        .getElementById("full-detail")
        ?.scrollIntoView({ behavior: "smooth" });
    });
  }, []);
  // activityFlash removed — agent.run.finished no longer drives home-page
  // refresh (see useWSEvents comment below). The accordion's own live
  // updates are the signal; FlashBorderBox receives a static key of 0.

  const refresh = useCallback(async () => {
    try {
      // We try a bunch of endpoints. Each is wrapped so a 404 / network
      // failure on one section doesn't cascade and leave the page blank.
      const [
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
        anomalyLatest,
        inFlightSynth,
        configRes,
      ] = await Promise.all([
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
        // SQLite state-DB file size — see argosy.api.routes.health.db_size.
        // The endpoint is mounted at /api/system/db-size (and also at root
        // for the watchdog). We tolerate the legacy `bytes` / `human` field
        // names so this code keeps working against older backends.
        fetch("/api/system/db-size", { cache: "no-store" })
          .then(async (r): Promise<string | null> => {
            if (!r.ok) return null;
            const j = (await r.json()) as DbSizeResponse;
            if (typeof j.size_human === "string") return j.size_human;
            if (typeof j.human === "string") return j.human;
            if (typeof j.size_bytes === "number")
              return humanBytes(j.size_bytes);
            if (typeof j.bytes === "number") return humanBytes(j.bytes);
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
        // EX2 anomaly-detection banner — most-recent report. Fails
        // gracefully when migration 0038 hasn't been applied yet or
        // no report exists (fresh install).  WS event `anomaly.detected`
        // triggers a refresh so the banner pops the moment a Discount
        // Bank statement reveals a missing fee-waiver discount.
        api.anomalyLatest(USER_ID).catch(() => null),
        // In-flight synthesis banner — backend returns 200+null when
        // nothing is running, so a swallowed network/404 just yields the
        // same shape.  Polled every 10 s by the effect below while
        // non-null so the phase counter ticks up live.
        api
          .planInFlightSynthesis(USER_ID)
          .catch(() => ({ in_flight_synthesis: null })),
        // Backend-derived config counts (agent fleet size). Fails gracefully
        // to null so the hero falls back to AGENT_FLEET_SIZE_FALLBACK.
        api.config().catch(() => null),
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
        anomalyReport: anomalyLatest,
        inFlightSynthesis: inFlightSynth?.in_flight_synthesis ?? null,
        fleetCount: configRes?.fleet_count ?? null,
        error: null,
      });
    } catch (e: unknown) {
      setData((prev) => ({ ...prev, error: String(e) }));
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
    // EX2 — fires after every event-driven OR daily anomaly check
    // that produced at least one Anomaly. Banner refreshes so the
    // user sees a RED card-2923-fee-waiver disappearance within
    // seconds of the statement ingest.
    "anomaly.detected",
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
  //
  // Status badge logic (softened for non-linear quarterly schedules):
  //
  //   target_shares_ytd > 0 AND shares_sold_ytd >= target_shares_ytd → ON PACE (success)
  //   under target by   < 20%                                        → ON PACE (success)
  //   under target by  >= 20%                                        → BEHIND PACE (warning)
  //   target_shares_ytd == 0                                         → neutral "—" (no badge)
  //
  // Prefer the agent's own ``on_track`` boolean when ahead-of-target —
  // the agent owns the schedule semantics. Below-target we apply the
  // 20% tolerance band locally so a small lag against a linear pro-rata
  // doesn't flash a warning when the actual plan cadence is back-loaded.
  const nvdaPace = data.planDraft?.nvda_pace ?? null;
  const nvdaSold = nvdaPace?.shares_sold_ytd ?? 0;
  const nvdaTargetYtd = nvdaPace?.target_shares_ytd ?? 0;
  // Render only when the backend served real pace data (the endpoint now
  // falls back to the current plan + sales history itself). Null/absent —
  // or a zero plan target, which leaves no denominator — HIDES the tile
  // entirely; never an empty "—" with a warning pill.
  const showNvdaPace = nvdaPace !== null && nvdaTargetYtd > 0;
  const nvdaPctSold = nvdaTargetYtd > 0 ? (nvdaSold / nvdaTargetYtd) * 100 : 0;
  const nvdaOnPace = (() => {
    if (!showNvdaPace) return false;
    if (nvdaPace!.on_track || nvdaSold >= nvdaTargetYtd) return true;
    const underPct = ((nvdaTargetYtd - nvdaSold) / nvdaTargetYtd) * 100;
    return underPct < 20;
  })();

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
      {/* ============================================================
          THE GREETING — the FM's opening word, the TOP panel (the old
          brand hero was redundant with the nav; the logo + live clock
          live in the greeting header now). How you stand, what I need
          from you, what I'm watching. Everything operational (banners,
          strips, tiles, system telemetry) is demoted BELOW it or into
          the collapsed Full-detail region at the bottom. Cash events
          reach needs_you via the period-directive loop (the old
          WindfallBanner is off the home page); verified/needs-confirm
          action items land in needs_you too, so ActionItemsWidget and
          AdvisorBriefCard live in Full detail.
          ============================================================ */}
      <FMGreetingCard
        userId={USER_ID}
        onShowFullDetail={showFullDetail}
        onLoaded={setGreeting}
      />

      {/* EX2 — anomaly-detection banner. Only renders when the latest
          report carries at least one RED anomaly (e.g. Card 2923's
          fee-waiver promotion disappearing) — quiet-by-design, below
          the greeting. */}
      {hasRedAnomaly(data.anomalyReport) ? (
        <AnomalyBanner report={data.anomalyReport!} />
      ) : null}

      {/* ============================================================
          YOUR MONEY — finances-first. Net worth, concentration,
          proposals, plan adherence, today's brief, NVDA sell pace
          and the Argonaut paper-trading P&L all live above the fold.
          These are what the household principal opens the app for;
          pure ops/debug telemetry is demoted into the collapsed
          "System health" section near the bottom.
          ============================================================ */}

      {/* FM charts — what a fund manager would actually show. Wealth
          trajectory (12mo actual from snapshot history + ~2y projected
          from the canonical scenario engine, dashed + shaded band) and
          deconcentration progress (actual NVDA % of book vs the plan's
          glide waypoints). Replaces the old sparkline tiles, whose
          concentration curve was a GENERATED plausible decline, not
          data. */}
      <section>
        <SectionHeader label="OVERVIEW" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <WealthTrajectoryCard userId={USER_ID} />
          <DeconcentrationCard userId={USER_ID} />
        </div>
      </section>

      {/* Plan adherence — status-led: the greeting's canonical on_plan
          computation (shared via onLoaded — no second fetch), the
          critique verdict WITH ITS AGE + the weekly_review loop's next
          fire from /api/jobs, an honest "refresh overdue" pill when the
          critique is older than the weekly cadence, and a material-
          change line when the state observer flagged an allocation /
          concentration move since the last critique. */}
      <section>
        <SectionHeader label="PLAN" count={1} />
        <PlanAdherenceCard
          userId={USER_ID}
          plan={data.plan}
          greeting={greeting}
        />
      </section>

      {/* T4.5 — Daily brief. Rendered ONLY when a brief exists; the old
          empty-state placeholder printed env-var/CLI instructions at the
          client, which is plumbing — a missing brief simply doesn't
          render. */}
      {data.brief?.content_md || data.brief?.summary_text ? (
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
                  : "Today's brief"}
              </CardTitle>
              <CardDescription>
                {data.brief?.run_at
                  ? `Generated ${new Date(data.brief.run_at).toLocaleString()}`
                  : null}
              </CardDescription>
            </CardHeader>
            <CardContent>
              {data.brief?.content_md ? (
                <pre className="whitespace-pre-wrap text-xs font-mono text-foreground tabular-nums">
                  {data.brief.content_md}
                </pre>
              ) : (
                <pre className="whitespace-pre-wrap text-xs font-mono text-muted-foreground tabular-nums">
                  {data.brief?.summary_text}
                </pre>
              )}
            </CardContent>
          </Card>
        </section>
      ) : null}

      {/* NVDA PACE tile — finance-relevant (sell-down schedule), so it
          stays in YOUR MONEY rather than the demoted System-health
          block. Rendered ONLY when the backend served pace data (it
          falls back to the current plan + sales history itself); when
          null/absent the tile is hidden entirely. */}
      {showNvdaPace ? (
        <section>
          <SectionHeader
            label="NVDA PACE"
            action={
              <StatusPill tone={nvdaOnPace ? "success" : "warning"} mono>
                {nvdaOnPace ? "ON PACE" : "BEHIND PACE"}
              </StatusPill>
            }
          />
          <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-2">
            <div className="flex items-center justify-between gap-4 flex-wrap">
              <div className="font-mono text-sm tabular-nums">
                {`${nvdaSold.toLocaleString()} / ${nvdaTargetYtd.toLocaleString()} shares sold YTD (plan target)`}
              </div>
              <div className="text-[11px] text-muted-foreground tabular-nums">
                {`${nvdaPctSold.toFixed(1)}% of plan target · `}
                {pctOfYearElapsed().toFixed(0)}% of year elapsed
              </div>
            </div>
            <ProgressBar
              pct={Math.max(0, Math.min(100, nvdaPctSold))}
              tone={nvdaOnPace ? "success" : "warning"}
            />
          </div>
        </section>
      ) : null}

      {/* ARGONAUT card — paper-trading P&L. Finance-relevant side
          experiment, kept in YOUR MONEY. Chart only renders when we
          have ≥2 snapshots. */}
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

      {/* ============================================================
          FULL DETAIL — everything demoted from the default view. The
          greeting is the client surface; this region keeps the
          operational components alive (demoted, not deleted): the
          red-flag strip, run banners, the proposals placeholder, the
          legacy Phase-2 brief card, internal pills, and the System
          health block (which stays collapsed inside). Children are
          lazy-mounted by <CollapsibleSection/> so none of it fetches
          until the client asks for it.
          ============================================================ */}
      <div id="full-detail" className="scroll-mt-6">
      <CollapsibleSection
        title="Full detail"
        summary={`engine ${engineActive ? "active" : "down"} · ${data.fleetCount ?? AGENT_FLEET_SIZE_FALLBACK} agents · ${CADENCE_LOOPS} cadence loops`}
        open={fullDetailOpen}
        onOpenChange={setFullDetailOpen}
      >

      {/* Internal pills — formerly in the brand hero. */}
      <div className="flex items-center gap-2 flex-wrap">
        <StatusPill tone="neutral" mono>
          v0.1.0
        </StatusPill>
        <StatusPill tone="neutral" mono>
          {data.fleetCount ?? AGENT_FLEET_SIZE_FALLBACK} agents
        </StatusPill>
        <StatusPill tone="neutral" mono>
          {CADENCE_LOOPS} cadence loops
        </StatusPill>
        <StatusPill tone="accent" mono>
          paper mode
        </StatusPill>
      </div>

      {/* Advisor brief — demoted from the default view: its greeting
          headline duplicated the FM greeting above. The card itself
          keeps bullets the greeting lacks (gap-tracker + daily-brief +
          watchlist stitch), so it lives on here rather than being
          deleted. */}
      <AdvisorBriefCard userId={USER_ID} />

      {/* Action items — the FULL dated to-do list from the plan. What
          actually needs the client now flows into the greeting's
          needs_you list (verified / needs-confirm items); this widget
          is the complete view, kept for drill-down. */}
      <ActionItemsWidget userId={USER_ID} />

      {/* Home Red-Flag Strip — one row per active monitor_flags entry.
          The greeting already projects the flags that matter to the
          client (needs-you / watching); this strip is the FULL surface,
          including internal data-gap observations. */}
      <RedFlagStrip userId={USER_ID} />

      {/* In-flight synthesis banner — "Synthesis #N in flight". The
          polling effect above ticks the phase counter every 10 s while
          non-null; the backend liveness reaper guarantees a dead run
          never renders here as in-flight. */}
      {data.inFlightSynthesis ? (
        <InFlightSynthesisBanner inFlight={data.inFlightSynthesis} />
      ) : null}

      {/* Fleet self-review banner — RED/AMBER counts per synthesis. */}
      {data.fleetReview ? (
        <FleetSelfReviewBanner report={data.fleetReview} />
      ) : null}

      {/* PROPOSALS — flashes border on proposal.created/updated WS events. */}
      <section>
        <SectionHeader label="PROPOSALS" />
        <FlashBorderBox flashKey={proposalFlash}>
          <div className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-xs text-muted-foreground font-mono">
            The action queue lives on{" "}
            <Link href="/inbox" className="text-info hover:underline">
              /inbox
            </Link>
          </div>
        </FlashBorderBox>
      </section>

      {/* Legacy Phase 2 brief summary card (usually empty). */}
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

      {/* SYSTEM HEALTH — ops/debug telemetry. Stays collapsed inside
          Full detail: engine/kill-switch/spend/DB tiles, cadence ticks,
          domain-KB freshness, and the fleet decision-activity
          accordion. */}
      <CollapsibleSection
        title="System health"
        summary={`engine ${engineActive ? "active" : "down"} · DB ${data.dbSize ?? "—"}`}
      >
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
            // ARMED = safety net engaged. The green dot alone reads as
            // "running / healthy" (the same convention as the Engine tile
            // immediately to its left), which conflicts with the operator
            // meaning here. Swap to a Shield icon so the safety semantics
            // come through visually; the tone (and tooltip) preserve the
            // green=safe / red=unsafe convention.
            icon={killSwitchArmed ? Shield : ShieldOff}
            tooltip={
              killSwitchArmed
                ? "Kill switch is ARMED — automated trades are blocked."
                : "Kill switch is DISARMED — automated trades may execute."
            }
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

      {/* ACTIVITY — fleet decision-grouped accordion with live WS
          cascade. Internal fleet telemetry, so it lives inside the
          collapsed System-health block and only mounts (and fires its
          own fetch) when the section is expanded. */}
      <section>
        <SectionHeader label="ACTIVITY" />
        <FlashBorderBox flashKey={0}>
          <DecisionAccordion userId={USER_ID} />
        </FlashBorderBox>
      </section>
      </CollapsibleSection>
      </CollapsibleSection>
      </div>

      {data.error && (
        <p className="text-sm text-error font-mono">{data.error}</p>
      )}
    </main>
  );
}

// ---------- Local presentational helpers --------------------------------

interface SystemTileProps {
  label: string;
  value: string;
  tone: "success" | "warning" | "error" | "neutral";
  pulse?: boolean;
  /**
   * Optional Lucide icon component to render in place of the default
   * status dot. Used for the Kill-switch tile so ARMED / DISARMED reads
   * as a safety affordance rather than a generic running/stopped lamp.
   */
  icon?: React.ComponentType<{ className?: string }>;
  /** Optional title-attribute tooltip on the value row. */
  tooltip?: string;
}

function SystemTile({ label, value, tone, pulse, icon: Icon, tooltip }: SystemTileProps) {
  const toneTextClass =
    tone === "success"
      ? "text-success"
      : tone === "warning"
        ? "text-warning"
        : tone === "error"
          ? "text-error"
          : "text-muted-foreground";
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
      <div className="flex items-center gap-2" title={tooltip}>
        {Icon ? (
          <Icon
            className={`h-4 w-4 shrink-0 ${toneTextClass} ${pulse ? "argosy-pulse-dot" : ""}`}
            aria-hidden
          />
        ) : (
          <span
            aria-hidden
            className={`inline-block h-2 w-2 rounded-full ${dotClass} ${
              pulse ? "argosy-pulse-dot" : ""
            }`}
          />
        )}
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
          status {inFlight.status.replace(/_/g, " ")} · a new draft will appear
          when complete (~30 min)
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


// ----------------------------------------------------------------------
// EX2 — anomaly banner: surfaces the first RED anomaly from the
// most-recent /api/anomalies/latest payload. The home-page guard
// (hasRedAnomaly) suppresses the banner whenever there are no RED
// items so users don't see a "phantom" alert from an old AMBER /
// YELLOW report. AMBER/YELLOW remain visible inside the viewer page.
// ----------------------------------------------------------------------

function hasRedAnomaly(report: AnomalyReportDTO | null): boolean {
  if (!report) return false;
  return (report.report?.anomalies ?? []).some((a) => a.severity === "RED");
}

interface AnomalyBannerProps {
  report: AnomalyReportDTO;
}

function AnomalyBanner({ report }: AnomalyBannerProps) {
  // First RED anomaly drives the banner copy; the rest live inside
  // the viewer page. Keeps the home banner small + scannable.
  const firstRed = (report.report?.anomalies ?? []).find(
    (a) => a.severity === "RED",
  );
  if (!firstRed) return null;

  const sev = report.severity_summary;
  const red = sev.RED ?? 0;
  const amber = sev.AMBER ?? 0;
  const yellow = sev.YELLOW ?? 0;
  const generatedLabel = report.triggered_at
    ? new Date(report.triggered_at).toLocaleString()
    : "—";

  // Topic — short label the user can scan. Prefer the watchlist entry
  // slug (always populated by the agent) over the full observation.
  const topic =
    firstRed.watchlist_entry_name || "anomaly detected";

  return (
    <section
      className="rounded-lg border border-border border-l-2 border-l-error/80 bg-card px-4 py-3 flex items-center justify-between gap-3 flex-wrap"
      data-slot="anomaly-banner"
    >
      <div className="flex flex-col gap-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span aria-hidden className="font-mono text-sm text-error">
            ⚠
          </span>
          <span className="font-mono text-sm font-semibold">
            Anomaly detected: {topic}
          </span>
          <span className="font-mono text-[10px] text-muted-foreground/80">
            #{report.id} · {report.triggered_by}
          </span>
        </div>
        <div className="font-mono text-xs text-muted-foreground">
          {firstRed.observation}
        </div>
        <div className="font-mono text-[11px] text-muted-foreground tabular-nums">
          last observed: {firstRed.last_seen || "—"} · suggested:{" "}
          {firstRed.suggested_action}
        </div>
        <div className="font-mono text-[11px] text-muted-foreground">
          generated {generatedLabel}
        </div>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <StatusPill tone="error" mono>
          RED {red}
        </StatusPill>
        {amber > 0 ? (
          <StatusPill tone="warning" mono>
            AMBER {amber}
          </StatusPill>
        ) : null}
        {yellow > 0 ? (
          <StatusPill tone="neutral" mono>
            YELLOW {yellow}
          </StatusPill>
        ) : null}
        <Link
          href={`/anomalies/${report.id}`}
          className="ml-2 font-mono text-xs text-info hover:underline"
        >
          view details -&gt;
        </Link>
      </div>
    </section>
  );
}


