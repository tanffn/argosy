"use client";

import { useCallback, useEffect, useState } from "react";

import { LiveClock } from "@/components/live-clock";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { SectionHeader } from "@/components/ui/section-header";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type AgentActivityRow,
  type DailyBriefDTO,
  type PlanCurrentDTO,
  type PortfolioSnapshotDTO,
} from "@/lib/api";
import { useWSEvents } from "@/lib/ws";

const USER_ID = "ariel";

// SDD §3.1 fleet size and §5.1 cadence-loop count. Hardcoded today; can later
// be sourced from a /config endpoint.
const AGENT_FLEET_SIZE = 17;
const CADENCE_LOOPS = 9;

interface HomeData {
  portfolio: PortfolioSnapshotDTO | null;
  plan: PlanCurrentDTO | null;
  brief: DailyBriefDTO | null;
  agents: AgentActivityRow[];
  error: string | null;
}

const initial: HomeData = {
  portfolio: null,
  plan: null,
  brief: null,
  agents: [],
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

export default function Home() {
  const [data, setData] = useState<HomeData>(initial);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [portfolio, plan, brief, agents] = await Promise.all([
        api.portfolioSnapshot(USER_ID).catch(() => null),
        api.planCurrent(USER_ID).catch(() => null),
        api.dailyBriefLatest(USER_ID).catch(() => null),
        api.agentActivity(USER_ID, 10).catch(() => ({ rows: [], next_since: null })),
      ]);
      setData({
        portfolio,
        plan,
        brief,
        agents: agents?.rows ?? [],
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

  // Refresh on relevant WS events.
  const lastEvent = useWSEvents(["daily_brief.ready", "agent.run.finished"]);
  useEffect(() => {
    if (lastEvent) refresh();
  }, [lastEvent, refresh]);

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

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      {/* Brand hero card */}
      <section
        className="relative rounded-xl border border-border bg-card overflow-hidden"
        data-slot="brand-hero"
      >
        {/* Subtle accent gradient strip along the top */}
        <div
          aria-hidden
          className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-emerald-500/40 via-cyan-500/40 to-transparent"
        />
        <div className="px-6 py-5 flex items-start justify-between gap-4 flex-wrap">
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

      {/* Compact metric row */}
      <section>
        <SectionHeader label="OVERVIEW" />
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-1">
            <div className="flex items-center justify-between">
              <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
                Net worth
              </span>
              <StatusPill tone="neutral" mono>
                liquid
              </StatusPill>
            </div>
            <div className="font-mono text-xl font-semibold tabular-nums">
              {loading ? "…" : `$${netWorth.toLocaleString()}K`}
            </div>
            <div className="text-[11px] text-muted-foreground">
              Δ vs prior — wired in Phase 4
            </div>
          </div>
          <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-1">
            <div className="flex items-center justify-between">
              <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
                Concentration
              </span>
              <StatusPill tone={concentrationTone} mono>
                NVDA
              </StatusPill>
            </div>
            <div className="font-mono text-xl font-semibold tabular-nums">
              {nvdaPct === null ? "—" : `${nvdaPct.toFixed(1)}%`}
            </div>
            <div className="text-[11px] text-muted-foreground">
              Sector caps wire in Phase 3
            </div>
          </div>
          <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-1">
            <div className="flex items-center justify-between">
              <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
                Pending proposals
              </span>
              <StatusPill tone="neutral" mono>
                idle
              </StatusPill>
            </div>
            <div className="font-mono text-xl font-semibold tabular-nums">0</div>
            <div className="text-[11px] text-muted-foreground">
              Proposals queue arrives in Phase 3
            </div>
          </div>
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
              <pre className="whitespace-pre-wrap text-xs font-mono text-muted-foreground">
                {data.brief?.summary_text || "(no daily brief on file)"}
              </pre>
            </CardContent>
          </Card>
        </div>
      </section>

      {/* Proposals (slot, count = 0 today) */}
      <section>
        <SectionHeader label="PROPOSALS" count={0} />
        <div className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-xs text-muted-foreground font-mono">
          No proposals queued · awaiting Phase 3
        </div>
      </section>

      {/* Activity */}
      <section>
        <SectionHeader label="ACTIVITY" count={data.agents.length} />
        {data.agents.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-xs text-muted-foreground font-mono">
            No agent runs yet.
          </div>
        ) : (
          <ul className="rounded-lg border border-border bg-card divide-y divide-border">
            {data.agents.map((row) => {
              const c = confidenceFor(row);
              return (
                <li
                  key={row.id}
                  className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm"
                >
                  <span className="flex items-center gap-3 min-w-0">
                    <span
                      aria-hidden
                      className={`inline-block h-2 w-2 rounded-full shrink-0 ${confidenceDot(c)}`}
                    />
                    <span className="font-mono font-bold w-40 truncate">
                      {row.agent_role}
                    </span>
                    <span className="text-xs text-muted-foreground truncate">
                      {row.model}
                    </span>
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
        )}
      </section>

      {data.error && (
        <p className="text-sm text-red-500 font-mono">{data.error}</p>
      )}
    </main>
  );
}
