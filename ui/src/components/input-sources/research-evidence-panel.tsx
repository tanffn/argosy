"use client";

import { useState, type FormEvent } from "react";

interface Evidence {
  id: string; source: string; url: string | null; scope: string;
  statement: string; published_at: string; verification: string;
}

export function ResearchEvidencePanel() {
  const [ticker, setTicker] = useState("");
  const [items, setItems] = useState<Evidence[]>([]);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  async function inspect(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setItems([]); setStatus("");
    try {
      const response = await fetch(`/api/input-sources/research?ticker=${encodeURIComponent(ticker.trim())}&user_id=ariel`, { cache: "no-store" });
      if (!response.ok) throw new Error("Could not load research evidence");
      const data = await response.json() as { ticker: string; items: Evidence[] };
      setItems(data.items);
      setStatus(`${data.ticker}: ${data.items.length} recent evidence items available for review`);
    } catch (error) { setStatus(error instanceof Error ? error.message : "Could not load research evidence"); }
    finally { setBusy(false); }
  }
  return <section className="rounded-xl border border-border p-5 space-y-4">
    <h2 className="text-lg font-semibold">Evidence for a holding or idea</h2>
    <p className="text-sm text-muted-foreground">Inspect the attributed claims and investor events available to Argosy’s stock reviews and research analysts. Market outlook requires an assessment of its relevance. These items are inputs, not Argosy verdicts.</p>
    <form onSubmit={inspect} className="flex gap-2">
      <label htmlFor="research-ticker" className="sr-only">Ticker</label>
      <input id="research-ticker" value={ticker} onChange={e => setTicker(e.target.value.toUpperCase())} placeholder="Ticker, e.g. NVDA" maxLength={32} className="rounded-md border border-border bg-background px-3 py-2" />
      <button disabled={busy || !ticker.trim()} className="rounded-md bg-primary text-primary-foreground px-3 py-2 disabled:opacity-50">{busy ? "Loading…" : "Inspect evidence"}</button>
    </form>
    <p role="status" className="text-sm text-muted-foreground">{status}</p>
    <ul className="space-y-3">{items.map(item => <li key={item.id} className="border-t border-border pt-3">
      <p className="text-xs text-muted-foreground">{item.source} · {item.scope === "market" ? "Market context" : "Company evidence"} · {item.published_at}</p>
      <p className="text-sm mt-1">{item.statement}</p>
      <p className="text-xs text-muted-foreground mt-1">{item.verification} {item.url && /^https?:\/\//i.test(item.url) && <a href={item.url} target="_blank" rel="noreferrer" className="underline">Original source</a>}</p>
    </li>)}</ul>
  </section>;
}
