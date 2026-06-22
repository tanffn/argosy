"use client";

/* eslint-disable react-hooks/set-state-in-effect --
 * Fetch-on-mount + WS-driven refetch pattern, consistent with the rest of the
 * app pending the planned React Query migration.
 */

/**
 * /inbox — the back-office action inbox.
 *
 * ONE question at a glance: "what, if anything, needs me right now?" The
 * "Needs you now" queue is a PURE projection of GET /api/inbox — the server
 * owns membership, rank, materiality, and the rank reason. The client only
 * maps each item's semantic action intent to the matching API call. Everything
 * else (what Argosy did for me, Tools, Explore, the deploy-cash tool) is
 * secondary and collapsed below.
 */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { InboxItemCard } from "@/components/inbox/InboxItemCard";
import { InboxDeferDialog } from "@/components/inbox/InboxDeferDialog";
import { QuietState } from "@/components/inbox/QuietState";
import { FunnelTransparencyCard } from "@/components/proposals/funnel-transparency-card";
import { DeployCashCard } from "@/components/proposals/DeployCashCard";
import { RebalanceReviewCard } from "@/components/proposals/RebalanceReviewCard";
import { ConsultRunner } from "@/components/consult/consult-runner";
import { DiscoveryCard } from "@/components/portfolio/discovery-card";
import { TrendRadarCard } from "@/components/portfolio/trend-radar-card";
import { SpeculativeMonitorCard } from "@/components/portfolio/speculative-monitor-card";
import { UnallocatedCashCard } from "@/components/portfolio/unallocated-cash-card";
import { WindfallCard } from "@/components/retirement/WindfallCard";
import {
  api,
  type DeploymentPlanDTO,
  type InboxActionDTO,
  type InboxFeedDTO,
  type InboxItemDTO,
  type ProposalDetail,
  type ReasoningTrailItem,
} from "@/lib/api";
import { useWSEvents } from "@/lib/ws";

const USER_ID = "ariel";

export default function InboxPage() {
  const [feed, setFeed] = useState<InboxFeedDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [selected, setSelected] = useState<ProposalDetail | null>(null);
  const [deferTarget, setDeferTarget] = useState<InboxItemDTO | null>(null);

  // Deploy-cash tool state (the full tool lives collapsed below; the cash queue
  // item links to it). Prefilled from detected idle cash.
  const [deployAmount, setDeployAmount] = useState<number>(0);
  const [deployPlan, setDeployPlan] = useState<DeploymentPlanDTO | null>(null);
  const [deployLoading, setDeployLoading] = useState(false);
  const [unallocatedUsd, setUnallocatedUsd] = useState<number>(0);
  const [deployLive, setDeployLive] = useState<boolean>(false);

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const f = await api.getInbox(USER_ID);
      setFeed(f);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Re-fetch on any proposal / fill event so the queue stays live.
  const lastEvt = useWSEvents([
    "proposal.created",
    "proposal.updated",
    "proposal.executed",
    "fill.received",
  ]);
  useEffect(() => {
    if (lastEvt) refresh();
  }, [lastEvt, refresh]);

  // --- deploy-cash tool wiring (ported) ---
  useEffect(() => {
    api
      .portfolioUnallocatedCashProposal(USER_ID)
      .then((r) => {
        if (r && r.excess_usd > 0) {
          setUnallocatedUsd(r.excess_usd);
          setDeployAmount(r.excess_usd);
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (deployAmount <= 0) {
      setDeployPlan(null);
      return;
    }
    setDeployLoading(true);
    api
      .deployCashPlan(USER_ID, deployAmount, deployLive)
      .then((p) => {
        if (!cancelled) setDeployPlan(p);
      })
      .finally(() => {
        if (!cancelled) setDeployLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [deployAmount, deployLive]);

  // --- map a semantic action intent to the matching API call ---
  const runAction = useCallback(
    async (item: InboxItemDTO, action: InboxActionDTO) => {
      const ref = item.source_refs[0];
      const intent = action.intent;

      // View-reasoning + defer open UI rather than mutate; handle them first.
      if (intent === "view_reasoning" && ref?.source === "trade_proposal") {
        try {
          const d = await api.proposalDetail(USER_ID, Number(ref.ref_id));
          setSelected(d);
        } catch (e: unknown) {
          setError(String(e));
        }
        return;
      }
      if (intent === "defer" && ref?.source === "action_proposal") {
        setDeferTarget(item);
        return;
      }
      if (intent === "review_cash") {
        document.getElementById("deploy-cash")?.scrollIntoView({ behavior: "smooth" });
        return;
      }
      if (intent === "defer" && ref?.source === "cash_detector") {
        return; // session-local dismiss; nothing to persist
      }

      setBusyId(item.id);
      setError(null);
      try {
        if (ref?.source === "trade_proposal") {
          const id = Number(ref.ref_id);
          if (intent === "approve") await api.proposalApprove(id, USER_ID, false);
          else if (intent === "reject") await api.proposalReject(id, USER_ID, "Rejected from inbox");
          else if (intent === "ask_deeper_review") await api.proposalEscalateTier(id, USER_ID, 1);
          else if (intent === "execute") await api.proposalExecute(id, USER_ID);
        } else if (ref?.source === "action_proposal") {
          const id = Number(ref.ref_id);
          if (intent === "accept") await api.acceptActionProposal(id, { userId: USER_ID });
          else if (intent === "dismiss")
            await api.rejectActionProposal(id, { userId: USER_ID, reason: "Dismissed from inbox" });
        } else if (ref?.source === "plan_action_item") {
          const fingerprint =
            typeof item.body.content_fingerprint === "string"
              ? item.body.content_fingerprint
              : "";
          if (intent === "mark_done")
            await api.planActionItemAck(USER_ID, ref.ref_id, fingerprint);
        }
        await refresh();
      } catch (e: unknown) {
        setError(String(e));
      } finally {
        setBusyId(null);
      }
    },
    [refresh],
  );

  const onDeferConfirm = useCallback(
    async (date: string, note: string) => {
      if (!deferTarget) return;
      const ref = deferTarget.source_refs[0];
      if (ref?.source !== "action_proposal") return;
      setBusyId(deferTarget.id);
      try {
        await api.deferActionProposal(Number(ref.ref_id), date, { userId: USER_ID, note });
        await refresh();
      } catch (e: unknown) {
        setError(String(e));
        throw e;
      } finally {
        setBusyId(null);
        setDeferTarget(null);
      }
    },
    [deferTarget, refresh],
  );

  const items = useMemo(() => feed?.items ?? [], [feed]);
  // Keep the queue focused: actionable buckets (overdue → opportunity, 1–5) are
  // "Needs you now"; low-risk observations (bucket 6) collapse below so a long
  // tail of notes never drowns the decisions. The OBSERVATION bucket ordinal is
  // 6 (see argosy/services/inbox/types.py PriorityBucket).
  const OBSERVATION_BUCKET = 6;
  const actionable = useMemo(
    () => items.filter((i) => (i.bucket ?? 99) < OBSERVATION_BUCKET),
    [items],
  );
  const observations = useMemo(
    () => items.filter((i) => i.bucket === OBSERVATION_BUCKET),
    [items],
  );

  return (
    <main className="max-w-4xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Inbox</h1>
        <p className="text-sm text-muted-foreground">
          What needs you now — Argosy handles the rest in the background.
        </p>
      </header>

      {error && <p className="text-sm text-error font-mono">{error}</p>}
      {loading && !feed && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}

      {/* Needs you now — the one prioritized queue (actionable buckets only). */}
      {feed && (
        <section className="flex flex-col gap-3">
          {feed.quiet ? (
            <QuietState liveness={feed.liveness} />
          ) : (
            <>
              <div className="flex items-baseline justify-between">
                <h2 className="text-lg font-semibold tracking-tight">
                  Needs you now
                </h2>
                <span className="text-sm text-muted-foreground">
                  {actionable.length} pending
                </span>
              </div>
              {actionable.length > 0 ? (
                <ul className="flex flex-col gap-3">
                  {actionable.map((it) => (
                    <li key={it.id}>
                      <InboxItemCard
                        item={it}
                        busy={busyId === it.id}
                        onAction={runAction}
                      />
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Nothing needs a decision right now.
                </p>
              )}
            </>
          )}
        </section>
      )}

      {/* Low-risk observations — collapsed so they never drown the decisions. */}
      {observations.length > 0 && (
        <CollapsibleSection
          title="Things Argosy noticed"
          summary={`${observations.length} low-risk note${observations.length === 1 ? "" : "s"} — no action needed`}
        >
          <ul className="flex flex-col gap-3">
            {observations.map((it) => (
              <li key={it.id}>
                <InboxItemCard
                  item={it}
                  busy={busyId === it.id}
                  onAction={runAction}
                />
              </li>
            ))}
          </ul>
        </CollapsibleSection>
      )}

      {/* Reasoning trail (opened from a trade's "See the reasoning"). */}
      {selected && (
        <Card className="border-primary/50">
          <CardHeader className="flex flex-row justify-between items-start">
            <div>
              <CardTitle>Reasoning trail</CardTitle>
              <CardDescription>
                {selected.proposal.action} {selected.proposal.ticker}
                {selected.decision_run &&
                  typeof (selected.decision_run as { id?: unknown }).id === "number" && (
                    <>
                      {" · "}
                      <a
                        href={`/decisions/${(selected.decision_run as { id: number }).id}`}
                        className="text-primary hover:underline"
                      >
                        view full replay →
                      </a>
                    </>
                  )}
              </CardDescription>
            </div>
            <Button variant="outline" size="sm" onClick={() => setSelected(null)}>
              Close
            </Button>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <section>
              <h3 className="text-sm font-semibold mb-2">
                Agent reports ({selected.reasoning_trail.length})
              </h3>
              <ul className="space-y-3">
                {selected.reasoning_trail.map((t: ReasoningTrailItem) => (
                  <li
                    key={t.id}
                    className="p-3 rounded-md border border-border/60 bg-muted/20"
                  >
                    <div className="flex justify-between items-center">
                      <span className="font-medium">{t.agent_role}</span>
                      <span className="text-xs font-mono text-muted-foreground">
                        {t.model} · {t.confidence ?? "?"}
                      </span>
                    </div>
                    <pre className="text-xs mt-2 font-mono whitespace-pre-wrap break-all">
                      {t.response_text.slice(0, 2000)}
                    </pre>
                  </li>
                ))}
              </ul>
            </section>
          </CardContent>
        </Card>
      )}

      <InboxDeferDialog
        open={deferTarget !== null}
        onOpenChange={(o) => {
          if (!o) setDeferTarget(null);
        }}
        title={deferTarget?.title ?? null}
        onConfirm={onDeferConfirm}
      />

      {/* ---- Secondary zones, collapsed: audit, tools, explore, cash tool ---- */}

      {/* What Argosy did for me — the daily decision-funnel transparency view.
          Renders nothing until the funnel has run; audit, not action. */}
      <FunnelTransparencyCard userId={USER_ID} />

      <CollapsibleSection
        title="Tools"
        summary="ask the analyst team · run a portfolio review"
      >
        <div className="flex flex-col gap-4">
          <RebalanceReviewCard userId={USER_ID} onReviewed={refresh} />
          <ConsultRunner />
        </div>
      </CollapsibleSection>

      {/* Deploy your cash — the full tool the cash queue item links to. The
          #deploy-cash and #allocation anchors are preserved so existing
          deep-links (Home banner, etc.) still resolve. */}
      <CollapsibleSection
        title="Deploy your cash"
        summary="how much is deployable and where it goes — a plan-bound buy list"
      >
        <div id="deploy-cash-flow" className="scroll-mt-6 flex flex-col gap-4">
          <UnallocatedCashCard userId={USER_ID} />
          <div id="deploy-cash" className="scroll-mt-6">
            <DeployCashCard
              plan={deployPlan}
              loading={deployLoading}
              amount={deployAmount}
              onAmountChange={setDeployAmount}
              unallocatedUsd={unallocatedUsd}
              userId={USER_ID}
              live={deployLive}
              onLiveChange={setDeployLive}
            />
          </div>
          <div id="allocation" className="scroll-mt-6">
            <WindfallCard showProposals={false} />
          </div>
        </div>
      </CollapsibleSection>

      <CollapsibleSection
        title="Explore"
        summary="high-potential discovery + the raw signals behind it"
      >
        <div className="space-y-4">
          <DiscoveryCard />
          <CollapsibleSection
            title="Raw sourcing (advanced)"
            summary="trend radar + exit monitor — the underlying signals"
          >
            <TrendRadarCard />
            <SpeculativeMonitorCard />
          </CollapsibleSection>
        </div>
      </CollapsibleSection>

      <p className="text-xs text-muted-foreground/70 text-center">
        Looking for something else? Try{" "}
        <Link href="/consult" className="text-primary hover:underline">
          Ask the team
        </Link>
        .
      </p>
    </main>
  );
}
