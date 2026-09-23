"use client";

/* eslint-disable react-hooks/set-state-in-effect -- load saved broker receipts on mount. */
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { api, type E2EOrderLineDTO, type FillItem, type FillSettlementDTO } from "@/lib/api";

const inputClass = "rounded border bg-background px-2 py-1 text-xs disabled:opacity-60";
const positive = (value: string) => value.trim() !== "" && Number.isFinite(Number(value)) && Number(value) > 0;
const nonnegative = (value: string) => value.trim() !== "" && Number.isFinite(Number(value)) && Number(value) >= 0;
const explicitTime = (value: string) => /T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value.trim()) && Number.isFinite(Date.parse(value));

export function ManualFillForm({ line, userId, onSaved }: {
  line: E2EOrderLineDTO; userId: string; onSaved: () => Promise<void>;
}) {
  const proposal = line.proposal!;
  const [receipts, setReceipts] = useState<FillItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<FillItem | null>(null);
  const [orderId, setOrderId] = useState(line.execution.broker_order_id ?? "");
  const [fillId, setFillId] = useState("");
  const [quantity, setQuantity] = useState(String(Math.max(0, proposal.target_quantity - line.execution.filled_quantity) || ""));
  const [price, setPrice] = useState("");
  const [commission, setCommission] = useState("");
  const [executedAt, setExecutedAt] = useState("");
  const [includeSettlement, setIncludeSettlement] = useState(false);
  const [currency, setCurrency] = useState("");
  const [withheld, setWithheld] = useState("");
  const [netCash, setNetCash] = useState("");
  const [reference, setReference] = useState("");
  const [listing, setListing] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const loadReceipts = useCallback(async () => {
    setLoading(true); setLoadError(null);
    try {
      const result = await api.fillsList(userId, proposal.id);
      setReceipts(result.rows.filter((row) => !row.paper));
      return result.rows.filter((row) => !row.paper);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally { setLoading(false); }
  }, [userId, proposal.id]);
  useEffect(() => { void loadReceipts(); }, [loadReceipts]);

  const choose = (receipt: FillItem | null) => {
    setSelected(receipt); setError(null); setMessage(null);
    setOrderId(receipt?.broker_order_id ?? line.execution.broker_order_id ?? "");
    setFillId(receipt?.external_fill_id ?? "");
    setQuantity(receipt ? String(receipt.quantity) : String(Math.max(0, proposal.target_quantity - line.execution.filled_quantity) || ""));
    setPrice(receipt ? String(receipt.price) : "");
    setCommission(receipt?.commission_confirmed && receipt.commission !== null ? String(receipt.commission) : "");
    setExecutedAt(receipt?.execution_time_confirmed ? receipt.filled_at : "");
    const facts = receipt?.settlement;
    setIncludeSettlement(Boolean(facts)); setCurrency(facts?.currency ?? "");
    setWithheld(facts ? String(facts.tax_withheld) : "");
    setNetCash(facts ? String(facts.net_cash_delta) : "");
    setReference(facts?.reference ?? ""); setListing(facts?.listing_symbol ?? "");
  };

  const applied = selected?.applied_snapshot_id != null;
  const savedSettlement = Boolean(selected?.settlement);
  const valid = orderId.trim() && fillId.trim() && positive(quantity) && positive(price)
    && (!commission.trim() || nonnegative(commission))
    && (!executedAt.trim() || explicitTime(executedAt))
    && (!includeSettlement || (currency && nonnegative(withheld) && nonnegative(commission)
      && netCash.trim() !== "" && Number.isFinite(Number(netCash)) && reference.trim() && explicitTime(executedAt)));

  const save = async () => {
    if (!valid || applied || busy) return;
    setBusy(true); setError(null); setMessage(null);
    try {
      const result = await api.proposalManualFill(proposal.id, {
        user_id: userId, broker_order_id: orderId.trim(), external_fill_id: fillId.trim(),
        quantity: Number(quantity), price: Number(price),
        commission: commission.trim() ? Number(commission) : null,
        filled_at: executedAt.trim() || null,
        settlement: includeSettlement ? {
          currency: currency as FillSettlementDTO["currency"], tax_withheld: withheld.trim(),
          net_cash_delta: netCash.trim(), reference: reference.trim(), listing_symbol: listing.trim() || null,
        } : null,
      });
      const updated = await loadReceipts();
      const savedReceipt = updated?.find((row) => row.id === result.fill_id);
      if (savedReceipt) choose(savedReceipt);
      setMessage(result.book_status === "applied"
        ? `Receipt saved. Shares and cash updated in snapshot #${result.applied_snapshot_id}. Tax-lot and statement verification remain separate.`
        : `Receipt saved; portfolio not updated. ${result.book_reason || "Settlement details need reconciliation."}`);
      // Capture succeeded even if the separate action-list refresh fails.
      try { await onSaved(); } catch { setError("Receipt saved, but the trade-plan display could not refresh. Reload to check its current status."); }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };

  return <div className="mt-2 space-y-2 rounded-md border border-border/60 bg-muted/20 p-2">
    <p className="text-xs font-medium">Record the actual {proposal.account_id} broker receipt</p>
    {loading && <p className="text-xs" role="status">Loading saved receipts…</p>}
    {loadError && <p className="text-xs text-error" role="alert">Could not load saved receipts. <Button size="sm" onClick={() => void loadReceipts()}>Retry receipts</Button></p>}
    {receipts.length > 0 && <ul className="space-y-1 text-xs" aria-label="Saved broker receipts">
      {receipts.map((receipt) => <li key={receipt.id} className="rounded border p-2">
        <span>{receipt.external_fill_id} · {receipt.quantity} shares · {receipt.applied_snapshot_id != null ? "Shares and cash updated" : "Not yet applied to portfolio"}</span>
        {receipt.applied_snapshot_id == null && <p className="text-muted-foreground">{receipt.book_reason || "Settlement details pending."}</p>}
        <Button size="sm" disabled={busy} onClick={() => choose(receipt)}>{receipt.applied_snapshot_id != null ? "View receipt" : "Complete receipt details"}</Button>
      </li>)}
    </ul>}
    {selected && <div className="text-xs">Editing saved execution {selected.external_fill_id}. Known facts are locked. <Button size="sm" disabled={busy} onClick={() => choose(null)}>New receipt</Button></div>}
    <fieldset disabled={busy || applied} className="space-y-2">
      <div className="grid gap-2 sm:grid-cols-3">
        <input aria-label="Broker order ID" placeholder="Broker order ID" value={orderId} disabled={Boolean(selected)} onChange={(e) => setOrderId(e.target.value)} className={inputClass} />
        <input aria-label="Execution ID" placeholder="Execution / fill ID" value={fillId} disabled={Boolean(selected)} onChange={(e) => setFillId(e.target.value)} className={inputClass} />
        <input aria-label="Filled quantity" type="number" step="any" placeholder="Quantity" value={quantity} disabled={Boolean(selected)} onChange={(e) => setQuantity(e.target.value)} className={inputClass} />
        <input aria-label="Fill price" type="number" step="any" placeholder="Actual price" value={price} disabled={Boolean(selected)} onChange={(e) => setPrice(e.target.value)} className={inputClass} />
        <input aria-label="Commission" type="number" step="any" placeholder="Commission (blank = unknown)" value={commission} disabled={selected?.commission_confirmed} onChange={(e) => setCommission(e.target.value)} className={inputClass} />
        <input aria-label="Execution timestamp with UTC offset" placeholder="2026-09-22T16:31:00+03:00" value={executedAt} disabled={selected?.execution_time_confirmed} onChange={(e) => setExecutedAt(e.target.value)} className={inputClass} />
      </div>
      <p className="text-xs text-muted-foreground">Use the broker execution time with its UTC offset, not today’s time. Blank fees or time remain unknown.</p>
      <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={includeSettlement} disabled={savedSettlement} onChange={(e) => setIncludeSettlement(e.target.checked)} />I have the broker’s settlement details</label>
      {includeSettlement && <div className="space-y-2 rounded border p-2">
        <p className="text-xs">Price, commission, withheld tax and net cash must all be in the selected currency. Use broker-reported amounts; do not estimate. For mixed currencies, save the receipt without settlement for reconciliation.</p>
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="text-xs">Settlement currency<select aria-label="Settlement currency" value={currency} disabled={savedSettlement} onChange={(e) => setCurrency(e.target.value)} className={`${inputClass} block w-full`}><option value="">Select currency</option><option value="NIS">NIS / ILS</option><option value="USD">USD</option><option value="EUR">EUR</option></select></label>
          <input aria-label="Tax withheld" type="number" step="any" placeholder="Tax withheld (enter 0 if confirmed)" value={withheld} disabled={savedSettlement} onChange={(e) => setWithheld(e.target.value)} className={inputClass} />
          <input aria-label="Net cash change" type="number" step="any" placeholder="Net cash: negative debit, positive credit" value={netCash} disabled={savedSettlement} onChange={(e) => setNetCash(e.target.value)} className={inputClass} />
          <input aria-label="Settlement reference" placeholder="Broker statement / settlement reference" value={reference} disabled={savedSettlement} onChange={(e) => setReference(e.target.value)} className={inputClass} />
          <input aria-label="Verified listing symbol" placeholder="Verified listing, e.g. CSPX.L (if known)" value={listing} disabled={Boolean(selected?.settlement?.listing_symbol)} onChange={(e) => setListing(e.target.value)} className={inputClass} />
        </div>
        <p className="text-xs text-muted-foreground">Withholding is not the final tax bill. New holdings also need their verified listing.</p>
      </div>}
      {!applied && <Button size="sm" disabled={busy || loading || Boolean(loadError) || !valid} onClick={() => void save()}>{busy ? "Recording…" : selected ? "Save receipt details" : "Record verified fill"}</Button>}
    </fieldset>
    <p className="text-xs text-muted-foreground">Ticker, side and account come from the approved order. This records an execution; it does not place a trade.</p>
    {message && <p className="text-xs" role="status">{message}</p>}
    {error && <p className="text-xs text-error" role="alert">{error}</p>}
  </div>;
}
