"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AgentCascadePanel } from "@/components/advisor/AgentCascadePanel";
import { Markdown } from "@/components/markdown";
import { AgentCascadeStrip } from "@/components/plan/agent-cascade-strip";
import { AgentReasoningDrawer } from "@/components/plan/agent-reasoning-drawer";
import { AllocationChart } from "@/components/plan/allocation-chart";
import { DeltaCard } from "@/components/plan/delta-card";
import { DeltaMap } from "@/components/plan/delta-map";
import { ActionsTimeline } from "@/components/plan/actions-timeline";
import { ExecutiveSummaryCard } from "@/components/plan/executive-summary-card";
import { ExportPlanButton } from "@/components/plan/export-plan-button";
import { HeadlineCard } from "@/components/plan/headline-card";
import { NvdaTrajectoryChart } from "@/components/plan/nvda-trajectory-chart";
import { CashflowProjectionChart } from "@/components/plan/cashflow-projection-chart";
import { SourcesHeatmap } from "@/components/plan/sources-heatmap";
import { SynthesisHealthBanner } from "@/components/plan/synthesis-health-banner";
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
  type FMObjection,
  type FMObjectionsResponse,
  type HorizonView,
  type InFlightSynthesisDTO,
  type AllocationGlidepathResponse,
  type NvdaTrajectoryResponse,
  type PlanCurrentDTO,
  type PortfolioSnapshotDTO,
  type RecapSummaryDTO,
  type TargetProgressResponse,
} from "@/lib/api";
import { friendlySourceLabels } from "@/lib/plain-english-labels";
import { derivePlanViewState } from "@/lib/plan-view-state";
import { formatLocalDateTime } from "@/lib/utils";
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
  const router = useRouter();
  const [plan, setPlan] = useState<PlanCurrentDTO | null>(null);
  // Wave 8 Piece E — DraftResponse-shape for the canonical current
  // plan so the recap surface can render horizon_long_md /
  // horizon_medium_md / horizon_short_md (the structured per-horizon
  // markdown emitted by the synthesizer). Fetched alongside the other
  // /plan resources; null when no current plan exists or the route
  // returns 404.
  const [planStructured, setPlanStructured] = useState<DraftResponse | null>(
    null,
  );
  // Wave 8 Piece G — three-line plain-English headline + four
  // at-a-glance blocks (deltas / portfolio total / insurance / audit).
  // Drives the HeadlineCard at the top of the recap_current layout.
  const [recapSummary, setRecapSummary] = useState<RecapSummaryDTO | null>(
    null,
  );
  // Wave 8 Piece B1 — allocation glidepath payload. Feeds the
  // ActionsTimeline (excluded non-pct targets) today; the Piece B2
  // chart consumes the same payload next.
  const [glidepath, setGlidepath] =
    useState<AllocationGlidepathResponse | null>(null);
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [objections, setObjections] = useState<FMObjectionsResponse | null>(null);
  // Live target-progress map keyed by item_id — fetched in parallel with
  // /api/plan/draft and forwarded to each TARGET DeltaCard so the
  // "current value · gap · status" strip renders. Null on 404 (no draft)
  // or transient fetch failure; the DeltaCard falls back to a muted
  // "(live state pending: synthesis required)" line in that case.
  const [targetProgress, setTargetProgress] =
    useState<TargetProgressResponse | null>(null);
  const [snapshot, setSnapshot] = useState<PortfolioSnapshotDTO | null>(null);
  const [nvda, setNvda] = useState<NvdaTrajectoryResponse | null>(null);
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

  // Snapshot of any plan-synthesis decision_run currently in flight,
  // sourced from /api/plan/in-flight-synthesis. Populated independently
  // of synthesisRunning so a synthesis triggered outside the UI (cron,
  // direct API call, another tab) still surfaces on /plan and locks the
  // "Run synthesis" button. Polled every 10 s while non-null because the
  // backend doesn't emit per-phase WS events; the polling loop is cheap
  // (one indexed DecisionRun lookup + one DecisionPhase count).
  const [inFlightSynthesis, setInFlightSynthesis] =
    useState<InFlightSynthesisDTO | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const planP = api.planCurrent(USER_ID).catch(() => null);
    const planStructuredP = api
      .planCurrentStructured(USER_ID)
      .catch(() => null);
    const recapP = api.planCurrentHeadline(USER_ID).catch(() => null);
    const glidepathP = api
      .planCurrentAllocationGlidepath(USER_ID)
      .catch(() => null);
    const draftP = api.planDraft(USER_ID).catch(() => null);
    const objP = api.planDraftObjections(USER_ID).catch(() => null);
    const snapP = api.portfolioSnapshot(USER_ID).catch(() => null);
    const nvdaP = api.planDraftNvdaTrajectory(USER_ID).catch(() => null);
    const progressP = api.planDraftTargetProgress(USER_ID).catch(() => null);
    // In-flight synthesis polling — returns 200 + null when nothing is
    // running, so a swallowed network error returns the same shape as
    // "no run". The polling effect below repeats this fetch every 10 s
    // while a run is in flight so the phase counter ticks up live.
    const inFlightP = api
      .planInFlightSynthesis(USER_ID)
      .catch(() => ({ in_flight_synthesis: null }));
    try {
      const [
        planV,
        planStructuredV,
        recapV,
        glidepathV,
        draftV,
        objV,
        snapV,
        nvdaV,
        inFlightV,
        progressV,
      ] = await Promise.all([
        planP,
        planStructuredP,
        recapP,
        glidepathP,
        draftP,
        objP,
        snapP,
        nvdaP,
        inFlightP,
        progressP,
      ]);
      setPlan(planV);
      setPlanStructured(planStructuredV);
      setRecapSummary(recapV);
      setGlidepath(glidepathV);
      setDraft(draftV);
      setObjections(objV);
      setSnapshot(snapV);
      setNvda(nvdaV);
      setInFlightSynthesis(inFlightV?.in_flight_synthesis ?? null);
      setTargetProgress(progressV);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll the in-flight synthesis endpoint while one is running so the
  // phase counter on the "Synthesis in flight" card ticks up live. The
  // backend doesn't emit per-phase WS events; we'd otherwise have to
  // wait for plan.draft.completed to know anything changed. 10 s cadence
  // is the spec'd interval; the route is cheap (indexed lookup + count)
  // so the polling load is negligible. The interval clears whenever
  // ``inFlightSynthesis`` flips back to null (synth completed or was
  // never running on the most recent refresh).
  useEffect(() => {
    if (inFlightSynthesis == null) return;
    const handle = window.setInterval(() => {
      api
        .planInFlightSynthesis(USER_ID)
        .then((r) => setInFlightSynthesis(r.in_flight_synthesis ?? null))
        .catch(() => {
          // Swallow transient errors — the next tick (or the next
          // refresh()) will recover. Don't surface a polling failure
          // as a banner error.
        });
    }, 10_000);
    return () => window.clearInterval(handle);
  }, [inFlightSynthesis]);

  // W10 — fetch-on-mount: ``refresh`` is a useCallback that fans out
  // to several setStates after awaiting REST. The eslint rule
  // ``react-hooks/set-state-in-effect`` flags this because it can't
  // see inside the closure, but this is the canonical "fetch initial
  // data on mount" pattern from the React docs (no Suspense data
  // source available here). Migrating to ``use()`` + a Suspense
  // boundary is a Plan-page rewrite, not a lint cleanup.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- justified: fetch-on-mount fan-out to setState; see comment above.
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

  // T2.4 — Resume a failed synthesis from the first incomplete phase.
  // The decision_audit_token has the form "plan-synth-N"; we parse N
  // out of it to address the backend's /resume route. The button only
  // makes sense when a synthesisError is set AND we have a token from
  // the run that just failed.
  const onResumeSynthesis = useCallback(async () => {
    if (!synthesisDecisionToken) return;
    const match = synthesisDecisionToken.match(/(\d+)$/);
    if (!match) return;
    const runId = Number(match[1]);
    setSynthesisError(null);
    setSynthesisRunning(true);
    setSynthesisDraftId(null);
    try {
      await api.advisorCheckInResume(USER_ID, runId);
      // Keep the existing decision_audit_token so the cascade panel
      // continues to filter on the same WS events.
    } catch (e: unknown) {
      setSynthesisError(e instanceof Error ? e.message : String(e));
      setSynthesisRunning(false);
    }
  }, [synthesisDecisionToken]);

  // Re-synthesize with the Fund Manager's objections fed back in as
  // guidance. This is the "fleet, fix it yourselves" loop the user asked
  // for — they shouldn't have to manually translate FM concerns into
  // synthesizer prompts. The guidance is a structured dump of every
  // objection (severity + topic + detail) prefixed with an instruction
  // telling the analysts + synthesizer to address each concern in the
  // next draft.
  // T4.7 — open the advisor page with the objection pre-seeded as the
  // user's question. The advisor agent reads user_context + plan
  // critiques on every turn, so it already has portfolio + identity
  // context; we just thread the specific FM objection in via a query
  // string the advisor page reads on mount and inserts as the first
  // user message.
  const onDiscussObjection = useCallback(
    (
      objection: { topic: string; detail: string; severity: string },
      objectionNumber: number,
    ) => {
      const seed = (
        `Fund Manager objection FM-Obj #${objectionNumber} on draft #${
          draft?.plan_version_id ?? "?"
        } ([${objection.severity}] — "${objection.topic}"):\n\n` +
        `${objection.detail}\n\n` +
        `Please explain what this means in plain English, what data the ` +
        `Fund Manager looked at to reach this conclusion, and what I should ` +
        `do about it. Walk me through any math step by step.`
      );
      const url = `/advisor?seed=${encodeURIComponent(seed)}`;
      router.push(url);
    },
    [draft, router],
  );

  const onResynthesizeWithObjections = useCallback(async () => {
    if (!objections || objections.objections.length === 0) return;
    setSynthesisError(null);
    setSynthesisRunning(true);
    setSynthesisDraftId(null);
    const guidance =
      "The prior draft was rejected by the Fund Manager. " +
      "Re-synthesize, explicitly addressing each of the following " +
      "objections. Resolve them or, where a constraint genuinely " +
      "can't be met (e.g. an expired statutory deadline), surface " +
      "that fact prominently in the rationale rather than papering " +
      "over it.\n\n" +
      objections.objections
        .map(
          (o, i) =>
            `${i + 1}. [${o.severity}] ${o.topic}\n   ${o.detail}`,
        )
        .join("\n\n");
    try {
      const r = await api.advisorCheckIn(USER_ID, guidance);
      setSynthesisDecisionToken(r.decision_audit_token);
    } catch (e: unknown) {
      setSynthesisError(e instanceof Error ? e.message : String(e));
      setSynthesisRunning(false);
    }
  }, [objections]);

  // Callback for the per-FM-objection agree/disagree flow's "Start new
  // round with my decisions" button. The endpoint composes its own
  // structured guidance from the user-state rows; we just need to wire
  // the returned decision_audit_token into the synthesis banner so the
  // page transitions cleanly to "synthesis running".
  const onStartNewRound = useCallback(
    (decisionAuditToken: string, _decisionRunId: number) => {
      void _decisionRunId; // captured by FMObjectionsCard via API; UI doesn't need
      setSynthesisError(null);
      setSynthesisDraftId(null);
      setSynthesisDecisionToken(decisionAuditToken);
      setSynthesisRunning(true);
    },
    [],
  );

  useWSEvents<{ user_id?: string; draft_id?: number }>(
    ["plan.draft.completed"],
    {
      onEvent: (e) => {
        if (e.payload.user_id !== USER_ID) return;
        // Always clear the in-flight banner + re-fetch — even when the
        // synthesis was kicked off outside this UI session (no
        // synthesisDecisionToken set), the user is staring at a page
        // that should now show a fresh draft. The early-return below
        // is gated on synthesisDecisionToken only so we don't try to
        // populate the kickoff-banner draftId for a run we didn't
        // start ourselves.
        setInFlightSynthesis(null);
        if (synthesisDecisionToken === null) {
          refresh();
          return;
        }
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

  const onRejectDelta = useCallback(
    async (delta: DeltaItem) => {
      if (!draft) return;
      const reason = window.prompt(
        `Reject "${delta.summary}" — what should the fleet know? (optional)`,
      ) ?? "";
      setWorking(true);
      setError(null);
      try {
        await api.planDraftDeltaReject(
          draft.plan_version_id,
          delta.item_id,
          USER_ID,
          reason,
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

  // T4.3 — per-delta slim re-debate state. Maps the item_id of a
  // delta currently being re-debated to the backend's decision_run_id
  // so the DeltaCard can show "Re-debate running…" and the user can
  // drill into /decisions/<id>. The map clears (per-item) when the
  // matching WS ``plan.delta.pushback.completed`` arrives.
  const [pushbackRuns, setPushbackRuns] = useState<
    Record<string, { decisionRunId: number; status: "running" | "completed" | "failed" }>
  >({});

  const onPushBackDelta = useCallback(
    async (delta: DeltaItem) => {
      if (!draft) return;
      const feedback = window.prompt(
        `Push back on "${delta.summary}" — what's missing or wrong? (required)`,
      ) ?? "";
      if (!feedback.trim()) return;
      setWorking(true);
      setError(null);
      try {
        const resp = await api.planDraftDeltaPushback(
          draft.plan_version_id,
          delta.item_id,
          USER_ID,
          feedback.trim(),
        );
        if (resp.decision_run_id != null) {
          // Capture the in-flight run_id so the DeltaCard can render a
          // "Re-debate running…" indicator. The WS subscription below
          // flips it to "completed" / "failed" when the slim flow
          // finishes (or errors).
          setPushbackRuns((prev) => ({
            ...prev,
            [delta.item_id]: {
              decisionRunId: resp.decision_run_id as number,
              status: "running",
            },
          }));
        } else if (resp.status === "cost_cap_refused") {
          // Surface as a soft error so the user knows the slim flow
          // didn't fire (the user_edit_note IS persisted regardless).
          setError(
            `Pushback recorded, but slim re-debate refused: ${resp.detail ?? "cost cap reached"}.`,
          );
        }
        await refresh();
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setWorking(false);
      }
    },
    [draft, refresh],
  );

  // T4.3 — subscribe to slim-redebate completion events. The backend
  // emits ``plan.delta.pushback.completed`` with ``user_id``,
  // ``item_id``, ``decision_run_id``, ``verdict`` once the
  // bull/bear/facilitator triad lands its verdict. Refresh the draft
  // so the user can see any updated state, and flip the per-item
  // status so the DeltaCard's badge changes from "running" to
  // "completed".
  useWSEvents<{
    user_id?: string;
    item_id?: string;
    decision_run_id?: number;
    verdict?: string;
    error?: string;
  }>(["plan.delta.pushback.completed"], {
    onEvent: (e) => {
      if (e.payload.user_id !== USER_ID) return;
      const iid = e.payload.item_id;
      if (typeof iid !== "string") return;
      setPushbackRuns((prev) => {
        const existing = prev[iid];
        if (!existing) return prev;
        return {
          ...prev,
          [iid]: {
            ...existing,
            status: e.payload.error ? "failed" : "completed",
          },
        };
      });
      refresh();
    },
  });

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
  // approved=null when no FM has actually evaluated this draft —
  // surfaces as "FM not evaluated" instead of silently rendering Approved.
  const fmNotEvaluated =
    objections != null && objections.approved === null;
  const fmVerdictStatus = objections?.verdict_status ?? "evaluated";
  const draftDecisionToken =
    draft?.decision_run_id != null ? `plan-synth-${draft.decision_run_id}` : null;

  // Effective in-flight state — true if EITHER this UI session kicked
  // off a synthesis OR the backend reports a running plan_revision run
  // (which catches API/cron/other-tab kickoffs that synthesisRunning
  // would otherwise miss). Drives the "Run synthesis" button's disabled
  // + label state so the button never lies about what's happening.
  const anyInFlight =
    synthesisRunning || inFlightSynthesis != null;
  // Show the "Synthesis in flight" card whenever a synthesis is
  // running. Prior version gated this on `draft == null` so the card
  // hid itself when a pending draft was still visible — but that left
  // the user with no visible indicator that a new synthesis was
  // chewing in the background, only a tiny subtitle text most people
  // miss. The card is light enough to render alongside an existing
  // draft; it's the truth signal for "what is the fleet doing right
  // now."
  const showInFlightCard = inFlightSynthesis != null;

  // Wave 8 Piece A — explicit five-state discriminator. Drives every
  // page-level render gate so the post-Accept-All path lands on the
  // recap layout instead of falling through to a stale-draft view.
  // Pure logic + matrix tests live in ``@/lib/plan-view-state``.
  const viewState = derivePlanViewState({
    plan,
    draft,
    inFlightSynthesis,
  });
  // pending_draft_triage and stale_fallback_with_warning both surface
  // the draft-driven sections (ExecutiveSummaryCard, proposal tabs,
  // critique, sources). recap_current and in_flight_synthesis suppress
  // those even when the backend returns a fallback draft row, because
  // a current plan / running synthesis is the authoritative content.
  const renderDraftSurfaces =
    viewState === "pending_draft_triage" ||
    viewState === "stale_fallback_with_warning";

  return (
    <main
      className="max-w-6xl mx-auto p-6 flex flex-col gap-6"
      data-view-state={viewState}
    >
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Plan</h1>
          <p className="text-sm text-muted-foreground">
            {plan?.version_label
              ? `Active: ${plan.version_label}`
              : "No plan imported yet."}
            {renderDraftSurfaces && draft
              ? ` · ${
                  draft.effective_role && draft.effective_role !== "draft"
                    ? "last draft"
                    : "pending draft"
                } #${draft.plan_version_id}${
                  fmRejected
                    ? " (Fund Manager rejected)"
                    : fmNotEvaluated && fmVerdictStatus === "carried_over"
                      ? " (FM verdict not refreshed)"
                      : fmNotEvaluated
                        ? " (not FM-evaluated)"
                        : ""
                }`
              : ""}
            {inFlightSynthesis != null
              ? ` · synthesizing (#${inFlightSynthesis.decision_run_id})`
              : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ExportPlanButton userId={USER_ID} size="default" />
          <Button
            variant="default"
            onClick={onRunSynthesis}
            disabled={anyInFlight || !plan?.plan_version_id}
            title={
              !plan?.plan_version_id ? "Import a baseline plan first" : undefined
            }
          >
            {anyInFlight ? "Synthesizing…" : "Run synthesis"}
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
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm text-error font-mono">{synthesisError}</p>
          {synthesisDecisionToken && /\d+$/.test(synthesisDecisionToken) && (
            <Button
              size="sm"
              variant="outline"
              onClick={onResumeSynthesis}
              disabled={synthesisRunning}
            >
              {synthesisRunning ? "Resuming…" : "Resume from last phase"}
            </Button>
          )}
        </div>
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

      {/* "Synthesis in flight" card — surfaces a running plan_revision
          decision_run when no draft is currently pending (the previous
          draft was superseded by the kickoff and the new one isn't
          written yet). Without this card the page would render only
          the baseline plan markdown + the two header buttons, which
          looks like the page is broken while a 30-min synthesis runs
          in the background. The Drill-in link opens the live agent
          cascade tree under /decisions/<id>. */}
      {showInFlightCard && inFlightSynthesis && (
        <InFlightSynthesisCard inFlight={inFlightSynthesis} />
      )}

      {/* "Stale draft" banner — only when the backend's /api/plan/draft
          fell back to a superseded draft AND there is no canonical
          current plan to fall forward to. Wave 8 Piece A: when a
          current plan IS set, the recap branch hides the stale draft
          entirely instead of papering over it with this warning. */}
      {viewState === "stale_fallback_with_warning" && draft && (
        <div className="rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">
          <p>
            <strong>Showing the last completed draft</strong> — it was
            marked <code className="font-mono">{draft.effective_role}</code>{" "}
            by a later synthesis attempt that did not produce a fresh
            draft. The rich view below is read-only context; press{" "}
            <strong>Run synthesis</strong> to generate a new draft you
            can accept or reject.
          </p>
        </div>
      )}

      {/* T0.7 — Synthesis fleet health banner. Renders above the
          ExecutiveSummaryCard (which embeds FMObjectionsCard) so the user
          always sees how the fleet ran, even when FM approved (in which
          case the FMObjectionsCard suppresses itself). The banner itself
          short-circuits to null when the backend returns
          synthesis_health=null (legacy drafts or builder failures).
          Wave 8 Piece A: gated on renderDraftSurfaces so the recap and
          in-flight states don't surface health of a stale draft. */}
      {renderDraftSurfaces && draft && (
        <SynthesisHealthBanner
          health={draft.synthesis_health}
          decisionRunId={draft.decision_run_id}
        />
      )}

      {/* Section 1 — Executive summary (draft-driven surface). */}
      {renderDraftSurfaces && draft && (
        <ExecutiveSummaryCard
          draft={draft}
          objections={objections}
          userId={USER_ID}
          working={working}
          onAcceptAll={onAcceptAll}
          onRejectAll={onRejectAll}
          onResynthesize={onResynthesizeWithObjections}
          resynthesizing={anyInFlight}
          // Lock per-objection editing while a fresh synthesis is in
          // flight — the user will act on the new draft's objections,
          // not on the stale carried-over ones. Stances written against
          // the stale draft don't carry over to the fresh draft anyway
          // (the user_state table keys on plan_version_id).
          editingLocked={inFlightSynthesis != null}
          onDiscussObjection={onDiscussObjection}
          onStartNewRound={onStartNewRound}
        />
      )}

      {/* Section 2 — Visualizations. SourcesHeatmap is rendered at the
          bottom of the page (after Critique findings) so the top of the
          plan tab stays focused on the proposal + dynamics, not the
          citation audit trail.

          AllocationChart + DeltaMap overlay targets from horizon rows
          so they only make sense alongside a draft surface (Piece A:
          gated on renderDraftSurfaces). NvdaTrajectoryChart +
          CashflowProjectionChart read from identity_yaml + household
          state, so they render whenever there's any plan context. */}
      {(renderDraftSurfaces || plan?.plan_version_id) && (
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {renderDraftSurfaces && draft && (
            <AllocationChart snapshot={snapshot} draft={draft} />
          )}
          <NvdaTrajectoryChart data={nvda} />
          {renderDraftSurfaces && draft && <DeltaMap draft={draft} />}
          <CashflowProjectionChart userId={USER_ID} />
        </section>
      )}

      {/* Section 3 — Proposed changes by horizon */}
      {renderDraftSurfaces && draft && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Proposed changes</CardTitle>
            <CardDescription>
              Pre-stage your approvals — Accept / Reject here only flag the
              item on the draft, they don&apos;t apply changes to your
              current plan. Nothing is committed until you accept the whole
              draft (or trigger a re-synth). Use Push back to fire a slim
              re-debate on a single item.
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
                  userId={USER_ID}
                  onAccept={onAcceptDelta}
                  onReject={onRejectDelta}
                  onPushBack={onPushBackDelta}
                  onSourceClick={openDrawerForLabel}
                  disabled={working}
                  pushbackRuns={pushbackRuns}
                  priorRoundObjections={objections?.prior_round_objections}
                  targetProgress={targetProgress}
                />
              </TabsContent>
              <TabsContent value="medium" className="mt-3">
                <HorizonDeltaList
                  h={draft.horizon_medium}
                  userId={USER_ID}
                  onAccept={onAcceptDelta}
                  onReject={onRejectDelta}
                  onPushBack={onPushBackDelta}
                  onSourceClick={openDrawerForLabel}
                  disabled={working}
                  pushbackRuns={pushbackRuns}
                  priorRoundObjections={objections?.prior_round_objections}
                  targetProgress={targetProgress}
                />
              </TabsContent>
              <TabsContent value="short" className="mt-3">
                <HorizonDeltaList
                  h={draft.horizon_short}
                  userId={USER_ID}
                  onAccept={onAcceptDelta}
                  onReject={onRejectDelta}
                  onPushBack={onPushBackDelta}
                  onSourceClick={openDrawerForLabel}
                  disabled={working}
                  pushbackRuns={pushbackRuns}
                  priorRoundObjections={objections?.prior_round_objections}
                  targetProgress={targetProgress}
                />
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>
      )}

      {/* Section 4 — Agent cascade */}
      {renderDraftSurfaces && draft && draftDecisionToken && (
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
                      <p
                        className="text-xs text-muted-foreground mt-1"
                        title={f.cited_sources.join(", ")}
                      >
                        cite:{" "}
                        {friendlySourceLabels(f.cited_sources).join(", ")}
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

      {/* Section 6 — Cited sources by item (citation audit trail). Lives
          at the bottom of the page so the top stays focused on the
          proposal + dynamics; reviewers scan the heatmap last. */}
      {renderDraftSurfaces && draft && <SourcesHeatmap draft={draft} />}

      {/* Wave 8 Piece A — recap_current placeholder. The page lands
          here when a canonical current plan exists with no pending
          draft + no in-flight run (the post-Accept-All state). Pieces
          E / G / B / F / C / D fill this surface with the headline,
          markdown rendering, glidepath, actions timeline, and
          cashflow assumptions. Piece A ships only the routing + a
          minimal placeholder so the page renders SOMETHING the user
          can read instead of falling through to a stale-draft view. */}
      {viewState === "recap_current" && plan && (
        <>
          {recapSummary ? <HeadlineCard recap={recapSummary} /> : null}
          <ActionsTimeline
            structured={planStructured}
            glidepath={glidepath}
          />
          <RecapCurrentPlaceholder plan={plan} structured={planStructured} />
        </>
      )}

      {/* no_plan state — user has never imported a baseline plan AND
          there's no draft, no in-flight, no fallback. */}
      {viewState === "no_plan" && !loading && (
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
  userId: string;
  onAccept: (d: DeltaItem) => void | Promise<void>;
  onReject: (d: DeltaItem) => void | Promise<void>;
  onPushBack: (d: DeltaItem) => void | Promise<void>;
  onSourceClick: (label: string) => void;
  disabled?: boolean;
  // T4.3 — per-delta slim re-debate state. Keyed by ``item_id`` so the
  // DeltaCard can render a "Re-debate running" / "Re-debate done" pill
  // and a /decisions drill-in link.
  pushbackRuns?: Record<
    string,
    { decisionRunId: number; status: "running" | "completed" | "failed" }
  >;
  // Prior-round FM objections (from FMObjectionsResponse). Passed
  // through to each DeltaCard so "Blocker #N" / "Objection #N" tokens
  // in the rationale link to the matching prior objection.
  priorRoundObjections?: FMObjection[];
  // Live target-progress map keyed by item_id (from TargetProgressResponse).
  // The list looks each TARGET delta's item_id up here and forwards the
  // matching row to the DeltaCard for the "current · gap · status" strip.
  targetProgress?: TargetProgressResponse | null;
}

function HorizonDeltaList({
  h,
  userId,
  onAccept,
  onReject,
  onPushBack,
  onSourceClick,
  disabled,
  pushbackRuns,
  priorRoundObjections,
  targetProgress,
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
            userId={userId}
            disabled={disabled}
            onAccept={onAccept}
            onReject={onReject}
            onPushBack={onPushBack}
            onSourceClick={onSourceClick}
            pushbackRun={pushbackRuns?.[d.item_id] ?? null}
            priorRoundObjections={priorRoundObjections}
            targetProgress={targetProgress?.progress?.[d.item_id] ?? null}
          />
        </li>
      ))}
    </ul>
  );
}

interface InFlightSynthesisCardProps {
  inFlight: InFlightSynthesisDTO;
}

function InFlightSynthesisCard({ inFlight }: InFlightSynthesisCardProps) {
  // YYYY-MM-DD HH:mm in user-local time. Backend now sends UTC-tagged
  // ISO so the Date parse correctly localizes (was rendering UTC
  // directly when the suffix was missing — 06:42 instead of 09:42).
  const startedAtLabel = formatLocalDateTime(inFlight.started_at);
  const phaseElapsedLabel =
    inFlight.current_phase_elapsed_seconds != null
      ? formatElapsedMinutes(inFlight.current_phase_elapsed_seconds)
      : null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Synthesis #{inFlight.decision_run_id} in flight
        </CardTitle>
        <CardDescription>
          {startedAtLabel ? `started ${startedAtLabel} · ` : ""}
          phase {inFlight.completed_phases} of {inFlight.total_phases} complete
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {inFlight.current_phase != null && (
          <div
            className="rounded-md border border-info/30 bg-info/5 p-2.5 text-sm flex flex-col gap-0.5"
            data-slot="current-phase"
          >
            <span>
              <strong>
                Phase {inFlight.current_phase} of {inFlight.total_phases}
                {inFlight.current_phase_label
                  ? ` — ${inFlight.current_phase_label}`
                  : ""}
              </strong>{" "}
              <span className="text-muted-foreground">currently running</span>
              {phaseElapsedLabel ? (
                <span className="text-muted-foreground">
                  {" "}
                  · {phaseElapsedLabel}
                </span>
              ) : null}
            </span>
            {inFlight.current_phase_elapsed_seconds != null &&
              inFlight.current_phase_elapsed_seconds > 15 * 60 && (
                <span className="text-xs text-warning">
                  This phase has been running unusually long. The
                  Opus call may be in a retry loop on malformed JSON
                  — check uvicorn logs for{" "}
                  <code className="font-mono text-[11px]">
                    claude_code.malformed_json_retry
                  </code>
                  .
                </span>
              )}
          </div>
        )}
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground">
            A new draft will appear when complete (~30 min total).
          </p>
          <Link
            href={`/decisions/${inFlight.decision_run_id}`}
            className="text-sm text-primary hover:underline"
          >
            Drill in →
          </Link>
        </div>
      </CardContent>
    </Card>
  );
}

function formatElapsedMinutes(seconds: number): string {
  if (seconds < 60) return `${seconds}s elapsed`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s === 0 ? `${m} min elapsed` : `${m} min ${s}s elapsed`;
}

interface RecapCurrentPlaceholderProps {
  plan: PlanCurrentDTO;
  // Wave 8 Piece E — DraftResponse for the canonical current plan
  // (from /api/plan/current/structured). Carries horizon_long_md /
  // horizon_medium_md / horizon_short_md, which the recap renders
  // as the "Full plan" surface broken out by horizon. Null when the
  // structured route returned no plan or fell through.
  structured: DraftResponse | null;
}

/**
 * Wave 8 Piece A + E surface for the recap_current state. The header
 * card identifies the canonical current plan; the Full Plan section
 * renders the synthesizer's per-horizon markdown (long / medium /
 * short) via the shared <Markdown> component so prose, tables, and
 * lists read naturally instead of as a code dump. Pieces G (headline
 * card), B (allocation glidepath), F (actions timeline), C (cashflow
 * defaults), and D (Monte Carlo) compose into / above this surface in
 * later commits per the wave-8 ship order.
 */
function RecapCurrentPlaceholder({
  plan,
  structured,
}: RecapCurrentPlaceholderProps) {
  const horizons: Array<{
    key: "long" | "medium" | "short";
    title: string;
    md: string | null | undefined;
  }> = [
    {
      key: "long",
      title: "Long horizon (multi-year)",
      md: structured?.horizon_long_md,
    },
    {
      key: "medium",
      title: "Medium horizon (12–24 months)",
      md: structured?.horizon_medium_md,
    },
    {
      key: "short",
      title: "Short horizon (next 90 days)",
      md: structured?.horizon_short_md,
    },
  ];
  const horizonsWithContent = horizons.filter(
    (h) => h.md != null && h.md.trim() !== "",
  );
  return (
    <>
      {horizonsWithContent.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Full plan</CardTitle>
            <CardDescription>
              The synthesizer&apos;s per-horizon plan. Changes happen
              through a new synthesis round.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-6">
            {horizonsWithContent.map((h) => (
              <section key={h.key}>
                <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground mb-2">
                  {h.title}
                </h3>
                <Markdown>{h.md as string}</Markdown>
              </section>
            ))}
          </CardContent>
        </Card>
      ) : plan.raw_markdown ? (
        <Card>
          <CardHeader>
            <CardTitle>Full plan</CardTitle>
            <CardDescription>
              Source markdown — read-only. The per-horizon structured
              render is unavailable for this plan_version; falling back
              to the baseline markdown.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Markdown>{plan.raw_markdown}</Markdown>
          </CardContent>
        </Card>
      ) : null}
    </>
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
