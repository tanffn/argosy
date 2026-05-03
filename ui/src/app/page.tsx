"use client";

import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type AgentActivityRow,
  type DailyBriefDTO,
  type PlanCurrentDTO,
  type PortfolioSnapshotDTO,
} from "@/lib/api";
import { useWSEvents } from "@/lib/ws";

const USER_ID = "ariel";

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
  const planBadge =
    findings.find((f) => f.severity === "RED")
      ? { variant: "error" as const, label: "Plan: RED" }
      : findings.find((f) => f.severity === "YELLOW")
        ? { variant: "secondary" as const, label: "Plan: YELLOW" }
        : { variant: "success" as const, label: "Plan: GREEN" };

  // Concentration scorecard: pull NVDA % from positions.
  const totalUsdK = netWorth;
  const nvdaPos = data.portfolio?.positions.find((p) => p.symbol === "NVDA");
  const nvdaPct =
    totalUsdK > 0 && nvdaPos?.usd_value_k
      ? (nvdaPos.usd_value_k / totalUsdK) * 100
      : null;

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <section className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <CardHeader>
            <CardDescription>Net worth (liquid)</CardDescription>
            <CardTitle className="font-mono text-2xl">
              {loading ? "…" : `$${netWorth.toLocaleString()}K`}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-xs text-muted-foreground">
            Δ vs prior snapshot — wired in Phase 4
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Concentration</CardDescription>
            <CardTitle className="font-mono text-2xl">
              {nvdaPct === null ? "—" : `${nvdaPct.toFixed(1)}%`}
              <span className="text-sm text-muted-foreground"> NVDA</span>
            </CardTitle>
          </CardHeader>
          <CardContent className="text-xs text-muted-foreground">
            Sector caps wire in Phase 3
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Pending proposals</CardDescription>
            <CardTitle className="font-mono text-2xl">0</CardTitle>
          </CardHeader>
          <CardContent className="text-xs text-muted-foreground">
            Proposals queue arrives in Phase 3
          </CardContent>
        </Card>
      </section>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>Plan adherence</CardTitle>
              <Badge variant={planBadge.variant}>{planBadge.label}</Badge>
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
            <CardTitle>Today&apos;s brief</CardTitle>
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
      </section>

      <section>
        <Card>
          <CardHeader>
            <CardTitle>Recent agent activity</CardTitle>
            <CardDescription>Last {data.agents.length} runs</CardDescription>
          </CardHeader>
          <CardContent>
            {data.agents.length === 0 ? (
              <p className="text-sm text-muted-foreground">No agent runs yet.</p>
            ) : (
              <ul className="divide-y divide-border text-sm font-mono">
                {data.agents.map((row) => (
                  <li
                    key={row.id}
                    className="flex items-center justify-between py-2"
                  >
                    <span className="flex items-center gap-3">
                      <span className="inline-block w-32 truncate">
                        {row.agent_role}
                      </span>
                      <span className="text-muted-foreground">{row.model}</span>
                    </span>
                    <span className="text-muted-foreground text-xs">
                      {new Date(row.created_at).toLocaleTimeString()} · $
                      {row.cost_usd.toFixed(4)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </section>

      {data.error && (
        <p className="text-sm text-red-500 font-mono">{data.error}</p>
      )}
    </main>
  );
}
