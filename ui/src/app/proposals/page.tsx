"use client";

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
  type FillItem,
  type ProposalDetail,
  type ProposalListItem,
  type ReasoningTrailItem,
} from "@/lib/api";
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

export default function ProposalsPage() {
  const [rows, setRows] = useState<ProposalListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ProposalDetail | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

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

      {error && <p className="text-sm text-error font-mono">{error}</p>}
      {loading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {!loading && rows.length === 0 && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground flex flex-col gap-3 items-center">
            <p>No proposals match the selected filter.</p>
            <p>
              Proposals are produced by ticker-decision flows. Head to the{" "}
              <Link
                href="/decide"
                className="text-primary hover:underline font-medium"
              >
                Decide
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

      <ul className="flex flex-col gap-3">
        {rows.map((p) => (
          <li key={p.id}>
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
                <div className="flex items-center gap-2 mt-3">
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
                  {(p.status === "executed_paper" ||
                    p.status === "executed_live") && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => onShowFills(p.id)}
                    >
                      Fills
                    </Button>
                  )}
                </div>
                {fillsByProposal[p.id]?.length ? (
                  <div className="mt-3 border-t border-border/40 pt-3">
                    <h4 className="text-xs font-semibold mb-2">
                      Fills ({fillsByProposal[p.id].length})
                    </h4>
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
                        {fillsByProposal[p.id].map((f) => (
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
              </CardContent>
            </Card>
          </li>
        ))}
      </ul>

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
