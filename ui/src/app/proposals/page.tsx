"use client";

/* eslint-disable react-hooks/set-state-in-effect --
 * Fetch-on-mount + WS-driven refetch pattern. One effect kicks off the
 * initial proposal list pull; the other re-fires it on proposal.* events
 * from the /ws stream. Planned Suspense / React Query migration will
 * dissolve this — see SDD "fetch-on-mount" note.
 */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type ActionProposalDTO,
  type ActionProposalPayload,
  type FillItem,
  type ProposalDetail,
  type ProposalListItem,
  type ReasoningTrailItem,
} from "@/lib/api";
import { friendlySourceLabel } from "@/lib/plain-english-labels";
import { ActionProposalCard } from "@/components/proposals/ActionProposalCard";
import { CustomizeModal } from "@/components/proposals/CustomizeModal";
import { DeferModal } from "@/components/proposals/DeferModal";
import { RejectModal } from "@/components/proposals/RejectModal";
import { WindfallCard } from "@/components/retirement/WindfallCard";
import { HighPotentialSleeveCard } from "@/components/portfolio/high-potential-sleeve-card";
import { useWSEvents } from "@/lib/ws";

const USER_ID = "ariel";

type TierBadge = "default" | "secondary" | "destructive" | "outline" | "success" | "error";

function tierVariant(tier: string): TierBadge {
  switch (tier) {
    case "T0":
      return "outline";
    case "T1":
      return "default";
    case "T2":
      return "secondary";
    case "T3":
      return "error";
    default:
      return "outline";
  }
}

function statusVariant(status: string): TierBadge {
  if (status === "approved" || status === "executed_paper" || status === "executed_live")
    return "success";
  if (status === "rejected" || status === "blocked" || status === "expired")
    return "error";
  return "secondary";
}

// T4.2: conviction badge colour scheme. HIGH stands out; MEDIUM is the
// quiet default; LOW is hinted as outline so the user reads "lower
// signal" without it screaming.
function convictionVariant(conviction: string | null): TierBadge {
  if (conviction === "HIGH") return "success";
  if (conviction === "LOW") return "outline";
  return "secondary";
}

// ----------------------------------------------------------------------
// T4.2: shared building blocks
//
// ProposalActions = the bottom action row (approve/reject/escalate/...).
// Lifted out so the regular full-card and the expanded speculative card
// can reuse the exact same approve/reject/execute wiring without
// duplication. The pre-T4.2 flow lived inline in the single rendering
// path; both call sites now route through this component.
//
// ProposalCard = the full "regular proposal" card. Identical shape to
// the original layout (header + rationale + actions + fills table); we
// only extracted it so the speculative section could opt out of
// rendering it when collapsed.
// ----------------------------------------------------------------------

type ActionProps = {
  p: ProposalListItem;
  busy: number | null;
  fills: FillItem[] | undefined;
  onApprove: (id: number, tier: string) => void;
  onReject: (id: number) => void;
  onEscalate: (id: number) => void;
  onExpand: (id: number) => void;
  onExecute: (id: number) => void;
  onShowFills: (id: number) => void;
};

function ProposalActions({
  p,
  busy,
  fills,
  onApprove,
  onReject,
  onEscalate,
  onExpand,
  onExecute,
  onShowFills,
}: ActionProps) {
  return (
    <>
      <div className="flex items-center gap-2 mt-3 flex-wrap">
        <Button
          size="sm"
          onClick={() => onApprove(p.id, p.tier)}
          disabled={
            busy === p.id ||
            p.status === "approved" ||
            p.status === "executed_paper" ||
            p.status === "executed_live"
          }
        >
          Approve
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => onReject(p.id)}
          disabled={busy === p.id || p.status === "rejected"}
        >
          Reject
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => onEscalate(p.id)}
          disabled={busy === p.id || p.tier === "T3"}
        >
          Escalate tier
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => onExpand(p.id)}
        >
          Reasoning trail
        </Button>
        {p.status === "approved" && (
          <Button
            size="sm"
            variant="default"
            onClick={() => onExecute(p.id)}
            disabled={busy === p.id}
          >
            Execute now
          </Button>
        )}
        {(p.status === "executed_paper" || p.status === "executed_live") && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => onShowFills(p.id)}
          >
            Fills
          </Button>
        )}
      </div>
      {fills?.length ? (
        <div className="mt-3 border-t border-border/40 pt-3">
          <h4 className="text-xs font-semibold mb-2">Fills ({fills.length})</h4>
          <table className="w-full text-xs font-mono">
            <thead className="text-muted-foreground">
              <tr>
                <th className="text-left py-1">when</th>
                <th className="text-left py-1">mode</th>
                <th className="text-left py-1">broker</th>
                <th className="text-right py-1">qty</th>
                <th className="text-right py-1">price</th>
                <th className="text-right py-1">commission</th>
              </tr>
            </thead>
            <tbody>
              {fills.map((f) => (
                <tr key={f.id} className="border-t border-border/30">
                  <td className="py-1">{f.filled_at}</td>
                  <td className="py-1">{f.paper ? "paper" : "live"}</td>
                  <td className="py-1">{f.broker}</td>
                  <td className="py-1 text-right">{f.quantity}</td>
                  <td className="py-1 text-right">{f.price}</td>
                  <td className="py-1 text-right">{f.commission}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}

function ProposalCard(props: ActionProps) {
  const { p } = props;
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-3 justify-between">
          <div className="flex items-center gap-3">
            <Badge variant={tierVariant(p.tier)}>{p.tier}</Badge>
            <CardTitle className="text-base">
              {p.action.toUpperCase()} {p.ticker}
            </CardTitle>
            <Badge variant={statusVariant(p.status)}>{p.status}</Badge>
          </div>
          <div className="text-xs font-mono text-muted-foreground">
            #{p.id} · {p.account_class} · {p.confidence ?? "?"}
          </div>
        </div>
        <CardDescription className="font-mono">
          size {p.size_shares_or_currency} {p.size_units} ·{" "}
          {p.order_type} · {p.instrument}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p className="text-sm">{p.rationale_summary}</p>
        <ProposalActions {...props} />
      </CardContent>
    </Card>
  );
}

export default function ProposalsPage() {
  const [rows, setRows] = useState<ProposalListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ProposalDetail | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  // T4.2: track which speculative cards the user has expanded. Default
  // is "collapsed" — speculative cards show just conviction + top
  // citation until the user clicks to expand for the full rationale.
  const [expandedSpec, setExpandedSpec] = useState<Set<number>>(
    () => new Set(),
  );
  const toggleSpec = useCallback((id: number) => {
    setExpandedSpec((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // -------------------------------------------------------------------
  // Spec E commit #6 — Action proposals (the new section above the
  // existing trade-proposal / windfall lists). State + handlers live
  // here on the page so the modals + cards stay presentational.
  // -------------------------------------------------------------------
  const [actionProposals, setActionProposals] = useState<ActionProposalDTO[]>(
    [],
  );
  const [actionLoading, setActionLoading] = useState(true);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState<number | null>(null);
  const [deferTarget, setDeferTarget] = useState<ActionProposalDTO | null>(
    null,
  );
  const [rejectTarget, setRejectTarget] = useState<ActionProposalDTO | null>(
    null,
  );
  const [customizeTarget, setCustomizeTarget] =
    useState<ActionProposalDTO | null>(null);

  const refreshActionProposals = useCallback(async () => {
    try {
      setActionLoading(true);
      const r = await api.getActionProposals({
        userId: USER_ID,
        status: "open",
      });
      setActionProposals(r.rows);
    } catch (e: unknown) {
      setActionError(String(e));
    } finally {
      setActionLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshActionProposals();
  }, [refreshActionProposals]);

  const onActionAccept = useCallback(
    async (id: number) => {
      if (!window.confirm("Accept this proposal?")) return;
      setActionBusy(id);
      setActionError(null);
      try {
        await api.acceptActionProposal(id, { userId: USER_ID });
        await refreshActionProposals();
      } catch (e: unknown) {
        setActionError(String(e));
      } finally {
        setActionBusy(null);
      }
    },
    [refreshActionProposals],
  );

  const onActionDeferSubmit = useCallback(
    async (deferDate: string, note: string) => {
      if (!deferTarget) return;
      setActionBusy(deferTarget.id);
      setActionError(null);
      try {
        await api.deferActionProposal(deferTarget.id, deferDate, {
          userId: USER_ID,
          note,
        });
        await refreshActionProposals();
      } catch (e: unknown) {
        setActionError(String(e));
        throw e;
      } finally {
        setActionBusy(null);
      }
    },
    [deferTarget, refreshActionProposals],
  );

  const onActionRejectSubmit = useCallback(
    async (reason: string) => {
      if (!rejectTarget) return;
      setActionBusy(rejectTarget.id);
      setActionError(null);
      try {
        await api.rejectActionProposal(rejectTarget.id, {
          userId: USER_ID,
          reason,
        });
        await refreshActionProposals();
      } catch (e: unknown) {
        setActionError(String(e));
        throw e;
      } finally {
        setActionBusy(null);
      }
    },
    [rejectTarget, refreshActionProposals],
  );

  const onActionCustomizeSubmit = useCallback(
    async (customPayload: ActionProposalPayload) => {
      if (!customizeTarget) return;
      setActionBusy(customizeTarget.id);
      setActionError(null);
      try {
        await api.acceptActionProposal(customizeTarget.id, {
          userId: USER_ID,
          customPayload,
        });
        await refreshActionProposals();
      } catch (e: unknown) {
        setActionError(String(e));
        throw e;
      } finally {
        setActionBusy(null);
      }
    },
    [customizeTarget, refreshActionProposals],
  );

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const r = await api.proposalsList(USER_ID, statusFilter || undefined);
      setRows(r.rows);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Subscribe to proposal.* events
  const lastEvt = useWSEvents([
    "proposal.created",
    "proposal.updated",
    "proposal.executed",
    "fill.received",
  ]);
  useEffect(() => {
    if (lastEvt) refresh();
  }, [lastEvt, refresh]);

  const onApprove = useCallback(
    async (id: number, tier: string) => {
      const requiresSecond = tier === "T3";
      setBusy(id);
      try {
        await api.proposalApprove(
          id,
          USER_ID,
          requiresSecond,
        );
        await refresh();
      } catch (e: unknown) {
        setError(String(e));
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  const onReject = useCallback(
    async (id: number) => {
      setBusy(id);
      try {
        await api.proposalReject(id, USER_ID, "Rejected from dashboard");
        await refresh();
      } catch (e: unknown) {
        setError(String(e));
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  const onEscalate = useCallback(
    async (id: number) => {
      setBusy(id);
      try {
        await api.proposalEscalateTier(id, USER_ID, 1);
        await refresh();
      } catch (e: unknown) {
        setError(String(e));
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  const [fillsByProposal, setFillsByProposal] = useState<Record<number, FillItem[]>>({});

  const onExecute = useCallback(
    async (id: number) => {
      setBusy(id);
      try {
        const r = await api.proposalExecute(id, USER_ID);
        await refresh();
        // Refresh fills for that proposal.
        const fr = await api.fillsList(USER_ID, id);
        setFillsByProposal((prev) => ({ ...prev, [id]: fr.rows }));
        if (r.status === "rejected") {
          setError(`Execution rejected: ${r.reason}`);
        }
      } catch (e: unknown) {
        setError(String(e));
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  const onShowFills = useCallback(async (id: number) => {
    try {
      const fr = await api.fillsList(USER_ID, id);
      setFillsByProposal((prev) => ({ ...prev, [id]: fr.rows }));
    } catch (e: unknown) {
      setError(String(e));
    }
  }, []);

  const onExpand = useCallback(
    async (id: number) => {
      try {
        const d = await api.proposalDetail(USER_ID, id);
        setSelected(d);
      } catch (e: unknown) {
        setError(String(e));
      }
    },
    [],
  );

  const filterOptions = useMemo(
    () => [
      "",
      "draft",
      "cooling",
      "awaiting_human",
      "approved",
      "rejected",
      "executed_paper",
      "executed_live",
      "blocked",
      "expired",
      "cancelled",
    ],
    [],
  );

  // T4.2: split rows. Speculative candidates surface as proposals with
  // ``account_class === "limited"`` (the Argonaut bucket, per
  // proposal_lifecycle.create_speculative_proposal). Everything else is
  // a regular plan-derived proposal.
  const { regularRows, speculativeRows } = useMemo(() => {
    const reg: ProposalListItem[] = [];
    const spec: ProposalListItem[] = [];
    for (const r of rows) {
      if (r.account_class === "limited") spec.push(r);
      else reg.push(r);
    }
    return { regularRows: reg, speculativeRows: spec };
  }, [rows]);

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Proposals</h1>
          <p className="text-sm text-muted-foreground">
            Pending decisions across all tiers. Approve, reject, or escalate.
          </p>
        </div>
        <select
          className="bg-background border border-border rounded-md px-3 py-1.5 text-sm"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          {filterOptions.map((opt) => (
            <option key={opt || "all"} value={opt}>
              {opt || "All statuses"}
            </option>
          ))}
        </select>
      </header>

      {/* Spec E commit #6 — Action proposals section. Sits ABOVE the
          allocation / windfall section so the unified action-proposal
          queue (all 8 kinds: allocate / repatriate_currency /
          rebalance / replan_full / add_life_event_phase /
          update_plan_assumption / set_watchlist / note_only) is the
          first thing the user sees. The windfall / trade-proposal
          surfaces below remain unchanged. */}
      <section id="action-proposals" className="scroll-mt-6 flex flex-col gap-3">
        <header>
          <h2 className="text-lg font-semibold tracking-tight">
            Action proposals
          </h2>
          <p className="text-xs text-muted-foreground">
            System-proposed actions across all kinds. Accept / Defer /
            Reject / Customize per row — Argosy never executes; you
            decide.
          </p>
        </header>
        {actionError && (
          <p className="text-sm text-error font-mono">{actionError}</p>
        )}
        {actionLoading && (
          <p className="text-sm text-muted-foreground">Loading action proposals…</p>
        )}
        {!actionLoading && actionProposals.length === 0 && (
          <Card>
            <CardContent className="py-6 text-center text-sm text-muted-foreground">
              No open action proposals.
            </CardContent>
          </Card>
        )}
        {actionProposals.length > 0 && (
          <ul className="flex flex-col gap-3">
            {actionProposals.map((p) => (
              <li key={p.id}>
                <ActionProposalCard
                  proposal={p}
                  busy={actionBusy === p.id}
                  onAccept={() => onActionAccept(p.id)}
                  onDefer={() => setDeferTarget(p)}
                  onReject={() => setRejectTarget(p)}
                  onCustomize={() => setCustomizeTarget(p)}
                />
              </li>
            ))}
          </ul>
        )}
      </section>

      <DeferModal
        open={deferTarget !== null}
        onOpenChange={(o) => {
          if (!o) setDeferTarget(null);
        }}
        proposal={deferTarget}
        onConfirm={onActionDeferSubmit}
      />
      <RejectModal
        open={rejectTarget !== null}
        onOpenChange={(o) => {
          if (!o) setRejectTarget(null);
        }}
        proposal={rejectTarget}
        onConfirm={onActionRejectSubmit}
      />
      <CustomizeModal
        open={customizeTarget !== null}
        onOpenChange={(o) => {
          if (!o) setCustomizeTarget(null);
        }}
        proposal={customizeTarget}
        onConfirm={onActionCustomizeSubmit}
      />

      {/* Allocation actions surface — WindfallCard self-suppresses when no
          event is detected, so the section renders as an empty scroll
          target most of the time. Banner on Home + the unallocated-cash
          tile both deep-link here via /proposals#allocation. */}
      <section id="allocation" className="scroll-mt-6">
        <WindfallCard />
      </section>

      {/* High-potential satellite sleeve — the med-high-risk slice (≥5% of a
          cash deployment), conviction-weighted, blend vehicle (UCITS thematic
          core + single-name carve-out). */}
      <section id="high-potential" className="scroll-mt-6">
        <HighPotentialSleeveCard />
      </section>

      {error && <p className="text-sm text-error font-mono">{error}</p>}
      {loading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {!loading && rows.length === 0 && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground flex flex-col gap-3 items-center">
            <p>No proposals match the selected filter.</p>
            <p>
              Proposals are produced by ticker-decision flows. Head to the{" "}
              <Link
                href="/consult"
                className="text-primary hover:underline font-medium"
              >
                Consult
              </Link>{" "}
              tab to submit a ticker with your rationale and have the agent
              fleet generate a structured Buy/Sell/Hold proposal.
            </p>
            <p className="text-xs">
              From the CLI you can also run{" "}
              <code className="font-mono">argosy decide --ticker NVDA --tier T2</code>.
            </p>
          </CardContent>
        </Card>
      )}

      {/* T4.2: regular plan-derived proposals. Speculative (Argonaut /
          limited) proposals are rendered separately below so the user
          can scan their core plan deltas without speculative noise. */}
      {regularRows.length > 0 && (
        <ul className="flex flex-col gap-3">
          {regularRows.map((p) => (
            <li key={p.id}>
              <ProposalCard
                p={p}
                busy={busy}
                fills={fillsByProposal[p.id]}
                onApprove={onApprove}
                onReject={onReject}
                onEscalate={onEscalate}
                onExpand={onExpand}
                onExecute={onExecute}
                onShowFills={onShowFills}
              />
            </li>
          ))}
        </ul>
      )}

      {/* T4.2: speculative candidates section. Collapsed-by-default
          cards surface conviction + top citation; click to expand for
          full rationale and the existing approve/reject/execute flow. */}
      {speculativeRows.length > 0 && (
        <section className="flex flex-col gap-3">
          <div className="border-t border-border/40 pt-4">
            <h2 className="text-sm font-semibold tracking-tight">
              Speculative candidates (Argonaut / limited account)
            </h2>
            <p className="text-xs text-muted-foreground">
              High-conviction speculative trades separate from the core
              plan. Risk-capped, paper-by-default. Click a card to expand
              for full rationale and approve / reject.
            </p>
          </div>
          <ul className="flex flex-col gap-2">
            {speculativeRows.map((p) => {
              const expanded = expandedSpec.has(p.id);
              const topCitation = p.cited_sources?.[0] ?? null;
              const extraCitations = (p.cited_sources?.length ?? 0) - 1;
              return (
                <li key={p.id}>
                  <Card className="border-border/60">
                    <CardHeader
                      className="cursor-pointer select-none"
                      onClick={() => toggleSpec(p.id)}
                    >
                      <div className="flex items-center gap-3 justify-between">
                        <div className="flex items-center gap-3 flex-wrap">
                          <span
                            aria-hidden
                            className="text-xs font-mono text-muted-foreground w-3"
                          >
                            {expanded ? "▼" : "▶"}
                          </span>
                          <Badge variant={tierVariant(p.tier)}>{p.tier}</Badge>
                          <CardTitle className="text-base">
                            {p.action.toUpperCase()} {p.ticker}
                          </CardTitle>
                          <Badge variant={convictionVariant(p.conviction)}>
                            {p.conviction ?? "?"} conviction
                          </Badge>
                          <Badge variant={statusVariant(p.status)}>
                            {p.status}
                          </Badge>
                          {topCitation && (
                            <span className="text-xs font-mono text-muted-foreground">
                              src: {topCitation}
                              {extraCitations > 0 && ` +${extraCitations}`}
                            </span>
                          )}
                        </div>
                        <div className="text-xs font-mono text-muted-foreground">
                          #{p.id}
                        </div>
                      </div>
                      {!expanded && (
                        <CardDescription className="font-mono text-xs">
                          size {p.size_shares_or_currency} {p.size_units} ·{" "}
                          {p.order_type} · {p.instrument}
                        </CardDescription>
                      )}
                    </CardHeader>
                    {expanded && (
                      <CardContent>
                        <CardDescription className="font-mono mb-3">
                          size {p.size_shares_or_currency} {p.size_units} ·{" "}
                          {p.order_type} · {p.instrument}
                        </CardDescription>
                        <p className="text-sm">{p.rationale_summary}</p>
                        {p.cited_sources && p.cited_sources.length > 0 && (
                          <div className="mt-2 flex flex-wrap items-center gap-1">
                            <span className="text-xs text-muted-foreground">
                              Cited sources:
                            </span>
                            {p.cited_sources.map((s, i) => (
                              <Badge key={i} variant="outline" title={s}>
                                {friendlySourceLabel(s)}
                              </Badge>
                            ))}
                          </div>
                        )}
                        {/* Reuse the same action row as regular proposals so
                            the existing approve/reject/execute flow keeps
                            working unchanged. */}
                        <ProposalActions
                          p={p}
                          busy={busy}
                          fills={fillsByProposal[p.id]}
                          onApprove={onApprove}
                          onReject={onReject}
                          onEscalate={onEscalate}
                          onExpand={onExpand}
                          onExecute={onExecute}
                          onShowFills={onShowFills}
                        />
                      </CardContent>
                    )}
                  </Card>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {selected && (
        <Card className="border-primary/50">
          <CardHeader className="flex flex-row justify-between items-start">
            <div>
              <CardTitle>
                Reasoning trail · proposal #{selected.proposal.id}
              </CardTitle>
              <CardDescription>
                {selected.proposal.action} {selected.proposal.ticker} · tier{" "}
                {selected.proposal.tier}
                {/* Provenance Wave E: deep-link to the full negotiation replay
                    so the user can see the parsed verdicts + transcripts. */}
                {selected.decision_run &&
                  typeof (selected.decision_run as { id?: unknown }).id ===
                    "number" && (
                    <>
                      {" · "}
                      <a
                        href={`/decisions/${
                          (selected.decision_run as { id: number }).id
                        }`}
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
              <h3 className="text-sm font-semibold mb-2">History</h3>
              <ul className="text-xs font-mono space-y-1">
                {selected.history.map((h, i) => (
                  <li key={i} className="text-muted-foreground">
                    {String(h.transitioned_at)} · {String(h.status)} ·{" "}
                    {String(h.transitioned_by)} · {String(h.note)}
                  </li>
                ))}
              </ul>
            </section>
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
    </main>
  );
}
