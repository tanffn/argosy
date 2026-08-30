"use client";

/* eslint-disable react-hooks/set-state-in-effect -- fetch-on-mount mirrors the
 * Inbox page; state changes happen after the API promise resolves. */

import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type E2EOrderLineDTO, type E2EProofDTO } from "@/lib/api";

function usd(value: number, cents = false): string {
  return value.toLocaleString(undefined, {
    style: "currency", currency: "USD", maximumFractionDigits: cents ? 2 : 0,
  });
}

function stageLabel(stage: string): string {
  return ({
    not_run: "Not run", blocked: "Stopped", broken: "Broken linkage",
    ready_to_accept: "Validated", awaiting_order_approval: "Orders created",
    execution: "Execution", partially_filled: "Partial fills", complete: "Complete",
  } as Record<string, string>)[stage] ?? stage.replaceAll("_", " ");
}

function humanStatus(status: string): string {
  return status.replaceAll("_", " ");
}

function ManualFillForm({ line, userId, onSaved }: {
  line: E2EOrderLineDTO; userId: string; onSaved: () => Promise<void>;
}) {
  const proposal = line.proposal!;
  const remaining = Math.max(0, proposal.target_quantity - line.execution.filled_quantity);
  const [orderId, setOrderId] = useState(line.execution.broker_order_id ?? "");
  const [fillId, setFillId] = useState("");
  const [quantity, setQuantity] = useState(String(remaining || ""));
  const [price, setPrice] = useState("");
  const [commission, setCommission] = useState("0");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setBusy(true); setError(null);
    try {
      await api.proposalManualFill(proposal.id, {
        user_id: userId, broker_order_id: orderId.trim(),
        external_fill_id: fillId.trim(), quantity: Number(quantity),
        price: Number(price), commission: Number(commission || 0),
      });
      await onSaved();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };

  return (
    <div className="mt-2 rounded-md border border-border/60 bg-muted/20 p-2 space-y-2">
      <p className="text-xs font-medium">Record the actual {proposal.account_id} broker receipt</p>
      <div className="grid gap-2 sm:grid-cols-5">
        <input aria-label="Broker order ID" placeholder="Broker order ID" value={orderId} onChange={(e) => setOrderId(e.target.value)} className="rounded border bg-background px-2 py-1 text-xs" />
        <input aria-label="Execution ID" placeholder="Execution / fill ID" value={fillId} onChange={(e) => setFillId(e.target.value)} className="rounded border bg-background px-2 py-1 text-xs" />
        <input aria-label="Filled quantity" type="number" step="any" placeholder="Quantity" value={quantity} onChange={(e) => setQuantity(e.target.value)} className="rounded border bg-background px-2 py-1 text-xs" />
        <input aria-label="Fill price" type="number" step="any" placeholder="Actual price" value={price} onChange={(e) => setPrice(e.target.value)} className="rounded border bg-background px-2 py-1 text-xs" />
        <input aria-label="Commission" type="number" step="any" placeholder="Commission" value={commission} onChange={(e) => setCommission(e.target.value)} className="rounded border bg-background px-2 py-1 text-xs" />
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" disabled={busy || !orderId.trim() || !fillId.trim() || Number(quantity) <= 0 || Number(price) <= 0} onClick={() => void save()}>{busy ? "Recording…" : "Record verified fill"}</Button>
        <span className="text-xs text-muted-foreground">Ticker, side and account come from the approved order and cannot be edited.</span>
      </div>
      {error && <p className="text-xs text-error">{error}</p>}
    </div>
  );
}

export function ArgosyRunCard({
  userId,
  compact = false,
}: {
  userId: string;
  compact?: boolean;
}) {
  const [proof, setProof] = useState<E2EProofDTO | null>(null);
  const [cash, setCash] = useState("120000");
  const [allowSells, setAllowSells] = useState(true);
  const [fundingAccount, setFundingAccount] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const value = await api.e2eProof(userId); setProof(value);
  }, [userId]);

  useEffect(() => {
    void refresh().catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [refresh]);

  const run = async () => {
    setBusy("run"); setError(null);
    try {
      setProof(await api.runE2EProof({
        user_id: userId, cash_usd: Number(cash), allow_sells: allowSells,
        horizon_years_min: 1, horizon_years_max: 5,
      }));
    } catch (e: unknown) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(null); }
  };

  const accept = async () => {
    if (!proof?.artifact) return;
    setBusy("accept"); setError(null);
    try {
      await api.acceptActionProposal(proof.artifact.action_proposal_id, {
        userId, fundingAccountId: fundingAccount,
      });
      await refresh();
    } catch (e: unknown) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(null); }
  };

  const actOnOrder = async (line: E2EOrderLineDTO, action: "approve" | "execute") => {
    if (!line.proposal) return;
    setBusy(`${action}:${line.proposal.id}`); setError(null);
    try {
      if (action === "approve") await api.proposalApprove(line.proposal.id, userId, false);
      else await api.proposalExecute(line.proposal.id, userId);
      await refresh();
    } catch (e: unknown) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(null); }
  };

  const needsFundingAccount = (proof?.artifact?.funding.new_cash_usd ?? 0) > 0;
  const buyTotal = useMemo(() =>
    proof?.lines.filter((l) => ["BUY", "ADD"].includes(l.authored.action))
      .reduce((sum, l) => sum + l.authored.notional_usd, 0) ?? 0,
    [proof],
  );

  return (
    <Card className={compact ? "" : "border-primary/50"} data-testid="argosy-e2e-run">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div><CardTitle>{compact ? (proof?.stage === "blocked" || proof?.stage === "broken" ? "Trade plan needs repair" : "Approve this trade plan") : "Argosy run: decision → outcome"}</CardTitle><CardDescription>{proof?.headline ?? "Loading the persisted proof chain…"}</CardDescription></div>
          {proof && <Badge variant={proof.stage === "blocked" || proof.stage === "broken" ? "destructive" : "secondary"}>{stageLabel(proof.stage)}</Badge>}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {proof?.stage === "not_run" && (
          <div className="rounded-md border p-3 space-y-3">
            <p className="text-sm">Run the real portfolio + market + discovery team and persist one validated order sheet.</p>
            <div className="flex flex-wrap items-center gap-3">
              <label className="text-sm">New funds (USD) <input aria-label="New funds USD" type="number" min="1" value={cash} onChange={(e) => setCash(e.target.value)} className="ml-2 w-32 rounded border bg-background px-2 py-1" /></label>
              <label className="text-sm flex items-center gap-2"><input type="checkbox" checked={allowSells} onChange={(e) => setAllowSells(e.target.checked)} /> Allow sell-X-to-buy-Y when justified</label>
              <Button disabled={busy !== null || Number(cash) <= 0} onClick={() => void run()}>{busy === "run" ? "Team is reviewing…" : "Run Argosy now"}</Button>
            </div>
            <p className="text-xs text-muted-foreground">Horizon 1–5 years. This authors a proposal; it does not approve or execute trades.</p>
          </div>
        )}

        {!compact && proof && proof.checks.length > 0 && (
          <div className="grid gap-2 sm:grid-cols-4">{proof.checks.map((check) => (
            <div key={check.key} className="rounded border px-2 py-2 text-xs"><span className={check.passed ? "text-success" : "text-muted-foreground"}>{check.passed ? "✓" : "○"}</span>{" "}{check.label}</div>
          ))}</div>
        )}

        {proof?.artifact && <>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span>Run {proof.artifact.fingerprint.slice(0, 10)}</span><span>Generated {new Date(proof.artifact.generated_at).toLocaleString()}</span><span>Horizon {proof.artifact.horizon_years.join("–")} years</span><span>{proof.lines.length} actions · {proof.no_action.length} deliberate holds</span><span>{proof.artifact.team_telemetry.reports.length} LLM calls · {usd(proof.artifact.team_telemetry.total_cost_usd, true)}</span>
          </div>
          <div className="rounded-md border p-3"><div className="flex flex-wrap justify-between gap-2 text-sm"><strong>Funding reconciliation</strong><span>New cash {usd(proof.artifact.funding.new_cash_usd)} + net sales {usd(proof.artifact.funding.gross_sell_proceeds_usd - proof.artifact.funding.sell_tax_usd)} − costs/reserve {usd(proof.artifact.funding.sell_costs_usd + proof.artifact.funding.reserve_usd)} − uninvestable venue remainder {usd(proof.artifact.funding.rounding_residual_usd)} = buys {usd(buyTotal)}</span></div></div>

          {!compact && <div className="overflow-x-auto rounded-md border"><table className="w-full text-xs">
            <thead className="bg-muted/30 text-left text-muted-foreground"><tr><th className="p-2">Order</th><th className="p-2">Live evidence</th><th className="p-2">Why / stop</th><th className="p-2">Execution</th><th className="p-2">Outcome audit</th></tr></thead>
            <tbody>{proof.lines.map((line) => {
              const p = line.proposal;
              return <tr key={line.authored.symbol} className="border-t align-top">
                <td className="p-2 whitespace-nowrap"><strong>{line.authored.action} {line.authored.symbol}</strong><br />{line.authored.shares.toLocaleString()} shares · {usd(line.authored.notional_usd)}{line.authored.authored_notional_usd && Math.abs(line.authored.authored_notional_usd - line.authored.notional_usd) > 0.01 ? <><br /><span className="text-muted-foreground">rounded from {usd(line.authored.authored_notional_usd)}</span></> : null}<br /><span className="text-muted-foreground">{line.authored.venue} · {line.authored.stance_source}</span></td>
                <td className="p-2">{usd(line.authored.evidence.price_usd, true)}<br /><span className="text-muted-foreground">{line.authored.evidence.price_source}<br />{new Date(line.authored.evidence.price_as_of).toLocaleString()}{line.authored.evidence.incorporation_country ? ` · ${line.authored.evidence.incorporation_country}` : ""}</span></td>
                <td className="p-2 min-w-64"><strong>Thesis:</strong> {line.authored.thesis}<br /><strong>Falsifier:</strong> {line.authored.falsifier}<br /><strong>Catalyst:</strong> {line.authored.catalyst.description} ({line.authored.catalyst.due_date}){line.authored.tax ? <><br /><strong>After tax:</strong> {usd(line.authored.tax.net_proceeds_usd)} net ({usd(line.authored.tax.estimated_tax_usd)} tax){line.authored.tax.evidence_as_of ? <><br /><span className="text-muted-foreground">Tax evidence {line.authored.tax.evidence_as_of}{line.authored.tax.evidence_expires_on ? ` · expires ${line.authored.tax.evidence_expires_on}` : ""}</span></> : null}</> : null}{line.authored.staged_execution ? <><br /><strong>Current clip:</strong> execute by {line.authored.staged_execution.execute_by}; reassess {line.authored.staged_execution.next_review_date} before any next sale.<br /><span className="text-muted-foreground">Waypoint: {line.authored.staged_execution.shares_to_sell_by_next_waypoint.toLocaleString()} total shares by {line.authored.staged_execution.next_waypoint_date}; later clips are indicative and require reprice/review.</span></> : null}</td>
                <td className="p-2 min-w-48">{p ? <><Badge variant="outline">{humanStatus(p.status)}</Badge><br /><span className="text-muted-foreground">#{p.id} · {p.account_id}</span><br />Filled {line.execution.filled_quantity}/{p.target_quantity}{line.execution.vwap ? ` @ ${usd(line.execution.vwap, true)}` : ""}<div className="mt-2">{p.status === "awaiting_human" && <Button size="sm" disabled={busy !== null} onClick={() => void actOnOrder(line, "approve")}>Approve exact line</Button>}{p.status === "approved" && !line.execution.manual_fill_allowed && <Button size="sm" disabled={busy !== null} onClick={() => void actOnOrder(line, "execute")}>Execute now</Button>}</div>{line.execution.manual_fill_allowed && <ManualFillForm line={line} userId={userId} onSaved={refresh} />}</> : <span className="text-muted-foreground">Created only after unified approval</span>}</td>
                <td className="p-2 min-w-44">{line.calibration ? <><Badge variant="outline">{humanStatus(line.calibration.status)}</Badge><br />Due {new Date(line.calibration.due_at).toLocaleDateString()}<br /><span className="text-muted-foreground">Prediction #{line.calibration.prediction_id}</span>{line.calibration.outcomes.map((o, i) => <div key={i}>{o.kind}{o.pnl_pct !== null ? ` · ${o.pnl_pct.toFixed(2)}%` : ""}</div>)}</> : <span className="text-muted-foreground">Scheduled with materialization</span>}</td>
              </tr>;
            })}</tbody>
          </table></div>}

          {proof.stage === "ready_to_accept" && <div className="rounded-md border border-primary/40 p-3 space-y-2">
            <strong className="text-sm">Approve the complete plan</strong>
            {needsFundingAccount && <label className="block text-sm">Exact funding account <input list="argosy-known-accounts" value={fundingAccount} onChange={(e) => setFundingAccount(e.target.value)} placeholder="e.g. schwab-rsu" className="ml-2 rounded border bg-background px-2 py-1" /><datalist id="argosy-known-accounts">{proof.known_accounts.map((account) => <option key={account} value={account} />)}</datalist></label>}
            <Button disabled={busy !== null || (needsFundingAccount && !fundingAccount.trim())} onClick={() => void accept()}>{busy === "accept" ? "Approving exact orders…" : "Approve plan"}</Button>
            <p className="text-xs text-muted-foreground">One approval locks and approves every exact line. It does not place trades; execution and fill reconciliation remain separate.</p>
          </div>}

          <details className="rounded-md border p-3"><summary className="cursor-pointer text-sm font-medium">{compact ? "Audit details and " : ""}NO-ACTION ({proof.no_action.length})</summary>
            {compact && proof.checks.length > 0 && <div className="mt-2 grid gap-2 sm:grid-cols-3">{proof.checks.map((check) => <div key={check.key} className="rounded border px-2 py-2 text-xs"><span className={check.passed ? "text-success" : "text-muted-foreground"}>{check.passed ? "✓" : "○"}</span>{" "}{check.label}</div>)}</div>}
            <ul className="mt-2 space-y-1 text-xs">{proof.no_action.map((row) => <li key={row.symbol}><strong>{row.symbol}</strong> — {row.reason}</li>)}</ul>
          </details>
          {compact && proof.lines.filter((line) => line.execution.manual_fill_allowed).map((line) => <ManualFillForm key={line.authored.symbol} line={line} userId={userId} onSaved={refresh} />)}
        </>}

        {proof && (proof.self_audit.length > 0 || proof.stage === "complete") && <div className={`rounded-md border p-3 text-sm ${proof.self_audit.length ? "border-error/40" : "border-success/40"}`}><strong>Self-audit</strong>{proof.self_audit.length ? <ul className="mt-1 list-disc pl-5 text-xs">{proof.self_audit.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p className="mt-1 text-xs text-success">No structural, arithmetic, voice, materialization or telemetry-linkage failures.</p>}</div>}
        {error && <p className="text-sm text-error whitespace-pre-wrap">{error}</p>}
      </CardContent>
    </Card>
  );
}
