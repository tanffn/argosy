"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { AgentCascadePanel } from "@/components/advisor/AgentCascadePanel";
import { Markdown } from "@/components/markdown";
import { AgentCascadeStrip } from "@/components/plan/agent-cascade-strip";
import { AgentReasoningDrawer } from "@/components/plan/agent-reasoning-drawer";
import { AllocationChart } from "@/components/plan/allocation-chart";
import { DeltaCard } from "@/components/plan/delta-card";
import { DeltaMap } from "@/components/plan/delta-map";
import { ExecutiveSummaryCard } from "@/components/plan/executive-summary-card";
import { NvdaTrajectoryChart } from "@/components/plan/nvda-trajectory-chart";
import { ProjectionChart } from "@/components/plan/projection-chart";
import { SourcesHeatmap } from "@/components/plan/sources-heatmap";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  api,
  type DeltaItem,
  type DraftResponse,
  type FMObjectionsResponse,
  type HorizonView,
  type NvdaTrajectoryResponse,
  type PlanCurrentDTO,
  type PortfolioSnapshotDTO,
  type ProjectionResponse,
} from "@/lib/api";
import { useWSEvents } from "@/lib/ws";

const USER_ID = "ariel";

interface Finding {
  plan_item_ref: string;
  severity: "RED" | "YELLOW" | "GREEN";
  topic: string;
  summary: string;
  evidence: string[];
  cited_sources: string[];
  recommended_action: string | null;
}

interface CritiqueShape {
  plan_label?: string;
  overall_summary?: string;
  findings?: Finding[];
}

// Map a provenance label back to the agent_role string used in agent_reports.
// Inverse of the backend's _citation_to_provenance_label.
function provenanceLabelToAgentRole(label: string): string | null {
  switch (label) {
    case "TaxAnalyst":
      return "tax";
    case "ConcentrationAnalyst":
      return "concentration";
    case "NewsAnalyst":
      return "news";
    case "MacroAnalyst":
      return "macro";
    case "FXAnalyst":
      return "fx";
    case "FundamentalsAnalyst":
      return "fundamentals";
    case "SentimentAnalyst":
      return "sentiment";
    case "TechnicalAnalyst":
      return "technical";
    case "PlanSynthesizer":
      return "plan_synthesizer";
    case "PlanCritique":
      return "plan_critique";
    default:
      return null;
  }
}

export default function PlanPage() {
  const [plan, setPlan] = useState<PlanCurrentDTO | null>(null);
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [objections, setObjections] = useState<FMObjectionsResponse | null>(null);
  const [snapshot, setSnapshot] = useState<PortfolioSnapshotDTO | null>(null);
  const [nvda, setNvda] = useState<NvdaTrajectoryResponse | null>(null);
  const [projection, setProjection] = useState<ProjectionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Drawer state — opened by either source-chip clicks or cascade-node clicks.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerAgentRole, setDrawerAgentRole] = useState<string | null>(null);

  // "Run synthesis" button kickoff state.
  const [synthesisDecisionToken, setSynthesisDecisionToken] = useState<
    string | null
  >(null);
  const [synthesisRunning, setSynthesisRunning] = useState(false);
  const [synthesisDraftId, setSynthesisDraftId] = useState<number | null>(null);
  const [synthesisError, setSynthesisError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const planP = api.planCurrent(USER_ID).catch(() => null);
    const draftP = api.planDraft(USER_ID).catch(() => null);
    const objP = api.planDraftObjections(USER_ID).catch(() => null);
    const snapP = api.portfolioSnapshot(USER_ID).catch(() => null);
    const nvdaP = api.planDraftNvdaTrajectory(USER_ID).catch(() => null);
    const projP = api.planDraftProjection(USER_ID, 10).catch(() => null);
    try {
      const [planV, draftV, objV, snapV, nvdaV, projV] = await Promise.all([
        planP,
        draftP,
        objP,
        snapP,
        nvdaP,
        projP,
      ]);
      setPlan(planV);
      setDraft(draftV);
      setObjections(objV);
      setSnapshot(snapV);
      setNvda(nvdaV);
      setProjection(projV);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onRecritique = useCallback(async () => {
    setWorking(true);
    setError(null);
    try {
      await api.recritique(USER_ID);
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  }, [refresh]);

  const onRunSynthesis = useCallback(async () => {
    setSynthesisError(null);
    setSynthesisRunning(true);
    setSynthesisDraftId(null);
    try {
      const r = await api.advisorCheckIn(USER_ID);
      setSynthesisDecisionToken(r.decision_audit_token);
    } catch (e: unknown) {
      setSynthesisError(e instanceof Error ? e.message : String(e));
      setSynthesisRunning(false);
    }
  }, []);

  useWSEvents<{ user_id?: string; draft_id?: number }>(
    ["plan.draft.completed"],
    {
      onEvent: (e) => {
        if (e.payload.user_id !== USER_ID) return;
        if (synthesisDecisionToken === null) return;
        if (typeof e.payload.draft_id === "number") {
          setSynthesisDraftId(e.payload.draft_id);
        }
        setSynthesisRunning(false);
        refresh();
      },
    },
  );

  const onAcceptAll = useCallback(async () => {
    if (!draft) return;
    setWorking(true);
    setError(null);
    try {
      await api.planDraftAccept(draft.plan_version_id, USER_ID);
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  }, [draft, refresh]);

  const onRejectAll = useCallback(async () => {
    if (!draft) return;
    const reason = window.prompt("What should the fleet reconsider?") ?? "";
    if (!reason.trim()) return;
    setWorking(true);
    setError(null);
    try {
      await api.planDraftReject(draft.plan_version_id, USER_ID, reason);
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  }, [draft, refresh]);

  const onAcceptDelta = useCallback(
    async (delta: DeltaItem) => {
      if (!draft) return;
      setWorking(true);
      setError(null);
      try {
        await api.planDraftDeltaAccept(
          draft.plan_version_id,
          delta.item_id,
          USER_ID,
        );
        await refresh();
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setWorking(false);
      }
    },
    [draft, refresh],
  );

  const openDrawerForLabel = useCallback((label: string) => {
    const role = provenanceLabelToAgentRole(label);
    if (!role) return;
    setDrawerAgentRole(role);
    setDrawerOpen(true);
  }, []);

  const openDrawerForRole = useCallback((role: string) => {
    setDrawerAgentRole(role);
    setDrawerOpen(true);
  }, []);

  const critique = (plan?.latest_critique_json as CritiqueShape | null) ?? null;
  const findings = critique?.findings ?? [];

  const fmRejected = objections?.approved === false;
  const draftDecisionToken =
    draft?.decision_run_id != null ? `plan-synth-${draft.decision_run_id}` : null;

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Plan</h1>
          <p className="text-sm text-muted-foreground">
            {plan?.version_label
              ? `Active: ${plan.version_label}`
              : "No plan imported yet."}
            {draft
              ? ` · pending draft #${draft.plan_version_id}${fmRejected ? " (FM rejected)" : ""}`
              : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="default"
            onClick={onRunSynthesis}
            disabled={synthesisRunning || !plan?.plan_version_id}
            title={
              !plan?.plan_version_id ? "Import a baseline plan first" : undefined
            }
          >
            {synthesisRunning ? "Synthesizing…" : "Run synthesis"}
          </Button>
          <Button
            variant="outline"
            onClick={onRecritique}
            disabled={working || !plan?.plan_version_id}
          >
            {working ? "Working…" : "Re-critique now"}
          </Button>
        </div>
      </header>

      {error && <p className="text-sm text-error font-mono">{error}</p>}
      {synthesisError && (
        <p className="text-sm text-error font-mono">{synthesisError}</p>
      )}
      {loading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {synthesisDecisionToken !== null && (
        <AgentCascadePanel
          userId={USER_ID}
          turnId={null}
          decisionId={synthesisDecisionToken}
          isResolved={!synthesisRunning}
        />
      )}

      {!synthesisRunning && synthesisDraftId !== null && (
        <p className="text-sm">
          Draft #{synthesisDraftId} ready ·{" "}
          <Link href="/proposals" className="text-primary hover:underline">
            → Review draft on /proposals
          </Link>
        </p>
      )}

      {/* Section 1 — Executive summary */}
      {draft && (
        <ExecutiveSummaryCard
          draft={draft}
          objections={objections}
          working={working}
          onAcceptAll={onAcceptAll}
          onRejectAll={onRejectAll}
        />
      )}

      {/* Section 2 — Visualizations */}
      {draft && (
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <AllocationChart snapshot={snapshot} draft={draft} />
          <NvdaTrajectoryChart data={nvda} />
          <ProjectionChart data={projection} />
          <DeltaMap draft={draft} />
          <SourcesHeatmap draft={draft} />
        </section>
      )}

      {/* Section 3 — Proposed changes by horizon */}
      {draft && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Proposed changes</CardTitle>
            <CardDescription>
              Review each delta and accept individually, or use Accept all in
              the summary card above.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Tabs defaultValue="long">
              <TabsList>
                <TabsTrigger value="long">
                  Long ({draft.horizon_long?.deltas_from_prior.length ?? 0})
                </TabsTrigger>
                <TabsTrigger value="medium">
                  Medium ({draft.horizon_medium?.deltas_from_prior.length ?? 0})
                </TabsTrigger>
                <TabsTrigger value="short">
                  Short ({draft.horizon_short?.deltas_from_prior.length ?? 0})
                </TabsTrigger>
              </TabsList>
              <TabsContent value="long" className="mt-3">
                <HorizonDeltaList
                  h={draft.horizon_long}
                  onAccept={onAcceptDelta}
                  onSourceClick={openDrawerForLabel}
                  disabled={working}
                />
              </TabsContent>
              <TabsContent value="medium" className="mt-3">
                <HorizonDeltaList
                  h={draft.horizon_medium}
                  onAccept={onAcceptDelta}
                  onSourceClick={openDrawerForLabel}
                  disabled={working}
                />
              </TabsContent>
              <TabsContent value="short" className="mt-3">
                <HorizonDeltaList
                  h={draft.horizon_short}
                  onAccept={onAcceptDelta}
                  onSourceClick={openDrawerForLabel}
                  disabled={working}
                />
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>
      )}

      {/* Section 4 — Agent cascade */}
      {draft && draftDecisionToken && (
        <AgentCascadeStrip
          userId={USER_ID}
          decisionId={draftDecisionToken}
          fmRejected={fmRejected}
          onNodeClick={openDrawerForRole}
        />
      )}

      {/* Section 5 — Critique findings (unchanged) */}
      {critique && (
        <Card>
          <CardHeader>
            <CardTitle>Critique findings</CardTitle>
            <CardDescription>{critique.overall_summary}</CardDescription>
          </CardHeader>
          <CardContent>
            {findings.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No findings recorded.
              </p>
            ) : (
              <ul className="flex flex-col gap-3">
                {findings.map((f, i) => (
                  <li
                    key={`${f.plan_item_ref}-${i}`}
                    className="p-3 rounded-md border border-border/60 bg-muted/20"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{f.topic}</span>
                      <Badge variant={severityVariant(f.severity)}>
                        {f.severity}
                      </Badge>
                    </div>
                    <p className="text-xs text-muted-foreground mt-1">
                      {f.plan_item_ref}
                    </p>
                    <p className="text-sm mt-2">{f.summary}</p>
                    {f.evidence.length > 0 && (
                      <ul className="text-xs text-muted-foreground mt-2 list-disc list-inside">
                        {f.evidence.map((e, j) => (
                          <li key={j}>{e}</li>
                        ))}
                      </ul>
                    )}
                    {f.cited_sources.length > 0 && (
                      <p className="text-xs font-mono text-muted-foreground mt-1">
                        cite: {f.cited_sources.join(", ")}
                      </p>
                    )}
                    {f.recommended_action && (
                      <p className="text-xs mt-2">
                        <span className="font-semibold">Action:</span>{" "}
                        {f.recommended_action}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {/* No-draft empty state */}
      {!loading && !draft && plan?.raw_markdown ? (
        <Card>
          <CardHeader>
            <CardTitle>Plan document</CardTitle>
          </CardHeader>
          <CardContent>
            <Markdown>{plan.raw_markdown}</Markdown>
          </CardContent>
        </Card>
      ) : null}

      {!loading && !draft && !plan?.raw_markdown && (
        <p className="text-sm text-muted-foreground">
          Run <code>argosy ingest plan &lt;path&gt;</code> to import a plan, or
          click <em>Run synthesis</em> to generate a draft from your active
          plan.
        </p>
      )}

      <AgentReasoningDrawer
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
        userId={USER_ID}
        decisionId={draftDecisionToken}
        agentRole={drawerAgentRole}
      />
    </main>
  );
}

interface HorizonDeltaListProps {
  h: HorizonView | null;
  onAccept: (d: DeltaItem) => void | Promise<void>;
  onSourceClick: (label: string) => void;
  disabled?: boolean;
}

function HorizonDeltaList({
  h,
  onAccept,
  onSourceClick,
  disabled,
}: HorizonDeltaListProps) {
  if (!h || h.deltas_from_prior.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        No changes proposed for this horizon.
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-3">
      {h.deltas_from_prior.map((d) => (
        <li key={d.item_id}>
          <DeltaCard
            delta={d}
            disabled={disabled}
            onAccept={onAccept}
            onSourceClick={onSourceClick}
          />
        </li>
      ))}
    </ul>
  );
}

function severityVariant(
  severity: string,
): "default" | "secondary" | "destructive" | "success" | "error" | "outline" {
  switch (severity) {
    case "RED":
      return "error";
    case "YELLOW":
      return "secondary";
    case "GREEN":
      return "success";
    default:
      return "outline";
  }
}
