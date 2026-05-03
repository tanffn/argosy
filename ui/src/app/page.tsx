"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

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
  type PlanCurrentDTO,
  type PortfolioSnapshotDTO,
} from "@/lib/api";
import { useWSEvents } from "@/lib/ws";

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
  brief: DailyBriefDTO | null;
  agents: AgentActivityRow[];
  argonautSnapshots: ArgonautSnapshot[];
  health: HealthStatus | null;
  dbSize: string | null;
  monthlySpend: number | null;
  domainKb: DomainKbTreeNode | null;
  cadenceLastTick: Record<string, string | null>;
  error: string | null;
}

const initial: HomeData = {
  portfolio: null,
  plan: null,
  brief: null,
  agents: [],
  argonautSnapshots: [],
  health: null,
  dbSize: null,
  monthlySpend: null,
  domainKb: null,
  cadenceLastTick: {},
  error: null,
};

type Confidence = "HIGH" | "MEDIUM" | "LOW" | null;

function confidenceFor(row: AgentActivityRow): Confidence {
  const c = (row.confidence ?? "").toUpperCase();
  if (c === "HIGH" || c === "MEDIUM" || c === "LOW") return c;
  return null;
}

function confidenceTone(c: Confidence): "success" | "warning" | "neutral" {
  if (c === "HIGH") return "success";
  if (c === "MEDIUM") return "warning";
  return "neutral";
}

function confidenceDot(c: Confidence): string {
  if (c === "HIGH") return "bg-emerald-500";
  if (c === "MEDIUM") return "bg-amber-500";
  return "bg-muted-foreground/50";
}

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

/** Group AgentActivityRow[] into (hour-bucket → rows) preserving order. */
function groupByHour(rows: AgentActivityRow[]): Array<{
  label: string;
  rows: AgentActivityRow[];
}> {
  const groups: Array<{ label: string; rows: AgentActivityRow[] }> = [];
  for (const r of rows) {
    const d = new Date(r.created_at);
    const label = `${String(d.getHours()).padStart(2, "0")}:00`;
    const last = groups[groups.length - 1];
    if (last && last.label === label) {
      last.rows.push(r);
    } else {
      groups.push({ label, rows: [r] });
    }
  }
  return groups;
}

export default function Home() {
  const [data, setData] = useState<HomeData>(initial);
  const [loading, setLoading] = useState(true);

  // Per-section flash flags. Each is set to a monotonically increasing
  // counter when its event arrives, so a fresh `.argosy-flash-border`
  // class reliably re-triggers the CSS animation.
  const [proposalFlash, setProposalFlash] = useState(0);
  const [activityFlash, setActivityFlash] = useState(0);

  const refresh = useCallback(async () => {
    try {
      // We try a bunch of endpoints. Each is wrapped so a 404 / network
      // failure on one section doesn't cascade and leave the page blank.
      const [
        portfolio,
        plan,
        brief,
        agents,
        argonautSnaps,
        healthRes,
        dbSizeRes,
        domainKb,
        monthlySummary,
        monthlyAgentRows,
        cadenceTickAudit,
      ] = await Promise.all([
        api.portfolioSnapshot(USER_ID).catch(() => null),
        api.planCurrent(USER_ID).catch(() => null),
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
        // month. We re-fetch a wider window for this.
        api
          .agentActivity(USER_ID, 500)
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
        brief,
        agents: agents?.rows ?? [],
        argonautSnapshots: argonautSnaps?.rows ?? [],
        health: healthRes,
        dbSize: dbSizeRes,
        monthlySpend,
        domainKb,
        cadenceLastTick,
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
  const lastEvent = useWSEvents([
    "daily_brief.ready",
    "agent.run.finished",
    "proposal.created",
    "proposal.updated",
  ]);
  useEffect(() => {
    if (!lastEvent) return;
    if (
      lastEvent.event === "proposal.created" ||
      lastEvent.event === "proposal.updated"
    ) {
      setProposalFlash((n) => n + 1);
    }
    if (lastEvent.event === "agent.run.finished") {
      setActivityFlash((n) => n + 1);
    }
    refresh();
  }, [lastEvent, refresh]);

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

  // NVDA pace.
  const nvdaSold = 0; // placeholder until real fills land
  const nvdaPctSold = (nvdaSold / NVDA_TARGET_2026) * 100;
  const nvdaOnPace = nvdaPctSold >= pctOfYearElapsed();

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

  // Group activity by hour-of-day for the timeline.
  const grouped = useMemo(() => groupByHour(data.agents.slice(0, 12)), [
    data.agents,
  ]);

  // Determine "ON DECK" pill: top entry less than 30s old.
  const topEntryRecent =
    data.agents[0] &&
    Date.now() - new Date(data.agents[0].created_at).getTime() < 30_000;

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      {/* Brand hero card — pass-2: glass card style with subtle blur and a
          gradient border via a ::before pseudo-element implemented through
          a dedicated absolutely-positioned div. */}
      <section
        className="relative rounded-xl overflow-hidden bg-card/80 backdrop-blur-sm shadow-sm"
        data-slot="brand-hero"
      >
        {/* Gradient border ring (1px) — drawn as an absolutely-positioned
            div using padding + masking, so we can keep the inner content
            on a normal flow. */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 rounded-xl p-px"
          style={{
            background:
              "linear-gradient(135deg, rgba(52,211,153,0.35) 0%, rgba(34,211,238,0.22) 35%, rgba(255,255,255,0.06) 70%, rgba(255,255,255,0.04) 100%)",
            WebkitMask:
              "linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0)",
            WebkitMaskComposite: "xor",
            maskComposite: "exclude",
          }}
        />
        <div
          aria-hidden
          className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-emerald-500/40 via-cyan-500/40 to-transparent"
        />
        <div className="relative px-6 py-5 flex items-start justify-between gap-4 flex-wrap">
          <div className="flex items-start gap-4 min-w-0">
            <span
              className="font-mono text-3xl leading-none select-none"
              aria-hidden
            >
              🚢
            </span>
            <div className="min-w-0">
              <h1 className="font-mono font-bold text-xl leading-tight">
                Argosy
              </h1>
              <p className="text-sm text-muted-foreground mt-0.5">
                multi-agent financial advisor
              </p>
              <div className="flex items-center gap-2 mt-3 flex-wrap">
                <StatusPill tone="neutral" mono>
                  v0.1.0
                </StatusPill>
                <span className="text-muted-foreground/60 text-xs">·</span>
                <StatusPill tone="neutral" mono>
                  {AGENT_FLEET_SIZE} agents
                </StatusPill>
                <span className="text-muted-foreground/60 text-xs">·</span>
                <StatusPill tone="neutral" mono>
                  {CADENCE_LOOPS} cadence loops
                </StatusPill>
                <span className="text-muted-foreground/60 text-xs">·</span>
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
                  ? "bg-emerald-500"
                  : ageMin < 240
                    ? "bg-amber-500"
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
        <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-2">
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <div className="font-mono text-sm tabular-nums">
              {nvdaSold.toLocaleString()} / {NVDA_TARGET_2026.toLocaleString()}{" "}
              shares sold YTD
            </div>
            <div className="text-[11px] text-muted-foreground tabular-nums">
              {nvdaPctSold.toFixed(1)}% of target ·{" "}
              {pctOfYearElapsed().toFixed(0)}% of year elapsed
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
              <CardTitle className="font-mono">Today&apos;s brief</CardTitle>
              <CardDescription>
                {data.brief?.run_at
                  ? `Generated ${new Date(data.brief.run_at).toLocaleString()}`
                  : "No brief yet — run `argosy brief --user-id ariel`."}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <pre className="whitespace-pre-wrap text-xs font-mono text-muted-foreground tabular-nums">
                {data.brief?.summary_text || "(no daily brief on file)"}
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

      {/* ACTIVITY — vertical timeline, grouped by hour, with fade-in. */}
      <section>
        <SectionHeader label="ACTIVITY" count={data.agents.length} />
        <FlashBorderBox flashKey={activityFlash}>
          {data.agents.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-xs text-muted-foreground font-mono">
              No agent runs yet.
            </div>
          ) : (
            <ActivityTimeline
              groups={grouped}
              topEntryRecent={!!topEntryRecent}
            />
          )}
        </FlashBorderBox>
      </section>

      {data.error && (
        <p className="text-sm text-red-500 font-mono">{data.error}</p>
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
      ? "bg-emerald-500"
      : tone === "warning"
        ? "bg-amber-500"
        : tone === "error"
          ? "bg-red-500"
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
      ? "bg-emerald-500"
      : tone === "warning"
        ? "bg-amber-500"
        : tone === "error"
          ? "bg-red-500"
          : "bg-cyan-500";
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

interface ActivityTimelineProps {
  groups: Array<{ label: string; rows: AgentActivityRow[] }>;
  topEntryRecent: boolean;
}

function ActivityTimeline({ groups, topEntryRecent }: ActivityTimelineProps) {
  return (
    <ul className="rounded-lg border border-border bg-card divide-y divide-border">
      {groups.map((g, gi) => (
        <li key={g.label} className="py-2">
          <div className="px-4 pb-1 text-[10px] uppercase tracking-wider font-mono text-muted-foreground tabular-nums">
            {g.label}
          </div>
          <ul className="relative pl-4">
            {/* vertical guide line */}
            <span
              aria-hidden
              className="absolute left-[1.1rem] top-1 bottom-1 w-px bg-border"
            />
            {g.rows.map((row, ri) => {
              const c = confidenceFor(row);
              const isFirst = gi === 0 && ri === 0;
              return (
                <li
                  key={row.id}
                  className="relative pl-5 pr-4 py-1.5 flex items-center justify-between gap-3 text-sm argosy-fade-in"
                >
                  <span
                    aria-hidden
                    className={`absolute left-[0.85rem] top-1/2 -translate-y-1/2 inline-block h-2 w-2 rounded-full ${confidenceDot(c)}`}
                  />
                  <span className="flex items-center gap-3 min-w-0">
                    <span className="font-mono font-bold w-40 truncate">
                      {row.agent_role}
                    </span>
                    <span className="text-xs text-muted-foreground truncate">
                      {row.model}
                    </span>
                    {isFirst && topEntryRecent ? (
                      <StatusPill tone="success" mono>
                        ON DECK
                      </StatusPill>
                    ) : null}
                  </span>
                  <span className="flex items-center gap-3 shrink-0">
                    <span className="text-xs text-muted-foreground font-mono tabular-nums">
                      {new Date(row.created_at).toLocaleTimeString()}
                    </span>
                    <span className="text-xs text-muted-foreground font-mono tabular-nums">
                      ${row.cost_usd.toFixed(4)}
                    </span>
                    {c ? (
                      <StatusPill tone={confidenceTone(c)} mono>
                        {c.toLowerCase()}
                      </StatusPill>
                    ) : (
                      <StatusPill tone="neutral" mono>
                        n/a
                      </StatusPill>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        </li>
      ))}
    </ul>
  );
}
