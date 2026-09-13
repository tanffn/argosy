"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

interface ResearchSource {
  id: number; name: string; kind: string; reference: string; enabled: boolean;
  cadence_hours: number; priority: number; last_polled_at: string | null; last_error: string | null;
  stats: { items_collected: number; items_analyzed: number; queued: number; failed: number;
    tickers: string[]; claims: number; evaluated: number; accuracy: number | null;
    reviews_requested: number; verdict_changes_on_review: number; reviews_triaged: number; reviews_completed: number;
    reports_supplied: number; reports_citing: number; cost_usd: number; };
}
interface Item {
  id: string; title: string; url: string; status: string; error: string | null; summary: string | null;
  claims: { id: string; statement: string; ticker: string | null; due_at: string | null; outcome: { verdict: string; excess_return_pct?: number; reason?: string } | null }[];
  reviews: { ticker: string; state: string; reason: string; result: { action?: string; rationale?: string; decision_run_id?: number } }[];
}

async function request(path: string, init?: RequestInit) {
  const response = await fetch(path, { cache: "no-store", ...init });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.error || "Research request failed");
  return data;
}
const jsonBody = (body: unknown, method = "POST") => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export function SharedSourcesPanel() {
  const [sources, setSources] = useState<ResearchSource[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("rss");
  const [reference, setReference] = useState("");
  const [cadence, setCadence] = useState(24);
  const [priority, setPriority] = useState(50);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [document, setDocument] = useState("");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const load = useCallback(async () => {
    const data = await request("/api/input-sources/research/sources");
    setSources(data.sources);
  }, []);
  useEffect(() => { void load().catch(e => setMessage(e.message)); }, [load]);
  async function act(fn: () => Promise<void>) {
    setBusy(true); setMessage("");
    try { await fn(); await load(); } catch (e) { setMessage(e instanceof Error ? e.message : "Request failed"); }
    finally { setBusy(false); }
  }
  async function add(event: FormEvent) {
    event.preventDefault();
    await act(async () => {
      await request("/api/input-sources/research/sources", jsonBody({ name, kind, reference: kind === "sec13d" ? "all" : reference, cadence_hours: cadence, priority }));
      setName(""); setReference(""); setMessage("Source added. New items join the daily research queue.");
    });
  }
  async function inspect(id: number) {
    setSelected(id); setItems([]);
    await act(async () => { setItems((await request(`/api/input-sources/research/items?source_id=${id}`)).items); });
  }
  const selectedSource = sources.find(s => s.id === selected);
  return <section className="rounded-xl border border-border p-5 space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg font-semibold">Shared research sources</h2>
        <p className="text-sm text-muted-foreground mt-1">Checked daily at 14:00 Israel time. Up to three fleet analyses across all feeds per day, with at most one per source. Deferred items stay queued.</p></div>
      <button disabled={busy} onClick={() => void act(async () => {
        const data = await request("/api/jobs/youtube_subscriptions/run-now", { method: "POST" });
        setMessage(`Research job ${data.job_run_id} started. Refresh to see progress.`);
      })} className="rounded-md border border-border px-3 py-2 text-sm disabled:opacity-50">Sync and analyze</button>
    </div>
    <form onSubmit={add} className="grid gap-3 sm:grid-cols-2">
      <label className="text-sm">Source name<input value={name} onChange={e => setName(e.target.value)} required maxLength={256} className="block w-full rounded border border-border bg-background px-3 py-2 mt-1" placeholder="Fund or author name" /></label>
      <label className="text-sm">Source type<select value={kind} onChange={e => setKind(e.target.value)} className="block w-full rounded border border-border bg-background px-3 py-2 mt-1">
        <option value="rss">RSS / Atom — letters, articles, podcasts</option><option value="sec13d">SEC 13D — ownership plans</option><option value="sec13f">SEC 13F — manager position changes</option><option value="manual">Pasted research — letters or restricted sources</option>
      </select></label>
      {kind !== "sec13d" && <label className="text-sm">{kind === "sec13f" ? "Manager CIK" : kind === "manual" ? "Publisher name or website" : "Feed URL"}<input value={reference} onChange={e => setReference(e.target.value)} required className="block w-full rounded border border-border bg-background px-3 py-2 mt-1" /></label>}
      <div className="flex gap-3"><label className="text-sm">Check interval (hours)<input type="number" value={cadence} onChange={e => setCadence(Number(e.target.value))} min={1} max={2160} required className="block w-28 rounded border border-border bg-background px-3 py-2 mt-1" /></label>
        <label className="text-sm">Priority (0–100)<input type="number" value={priority} onChange={e => setPriority(Number(e.target.value))} min={0} max={100} required className="block w-28 rounded border border-border bg-background px-3 py-2 mt-1" /></label></div>
      <button disabled={busy} className="rounded-md bg-primary text-primary-foreground px-3 py-2 disabled:opacity-50 sm:col-span-2">Add source</button>
    </form>
    <p role="status" className="text-sm text-muted-foreground">{message}</p>
    <button onClick={() => void act(load)} disabled={busy} className="text-sm underline">Refresh statistics</button>
    <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr className="text-left border-b border-border"><th className="p-2">Source</th><th className="p-2">Analyzed / collected</th><th className="p-2">Tickers / claims</th><th className="p-2">Reviews completed / requested</th><th className="p-2">Reports citing / supplied</th><th className="p-2">Directional accuracy</th><th className="p-2">Controls</th></tr></thead>
      <tbody>{sources.map(s => <tr key={s.id} className="border-b border-border align-top">
        <td className="p-2"><button onClick={() => void inspect(s.id)} disabled={busy} className="font-medium underline">{s.name}</button><p className="text-xs text-muted-foreground">{s.kind} · {s.cadence_hours}h · priority {s.priority}</p>{s.last_error && <p className="text-xs text-red-400 mt-1">{s.last_error}</p>}</td>
        <td className="p-2">{s.stats.items_analyzed} / {s.stats.items_collected}<p className="text-xs text-muted-foreground">{s.stats.queued} queued · {s.stats.failed} failed · ${s.stats.cost_usd.toFixed(2)}</p></td>
        <td className="p-2" title={s.stats.tickers.join(", ")}>{s.stats.tickers.length} / {s.stats.claims}</td>
        <td className="p-2">{s.stats.reviews_completed} / {s.stats.reviews_requested}<p className="text-xs text-muted-foreground">{s.stats.verdict_changes_on_review} changed verdicts</p></td>
        <td className="p-2">{s.stats.reports_citing} / {s.stats.reports_supplied}</td>
        <td className="p-2">{s.stats.accuracy === null ? "Not yet scored" : `${Math.round(s.stats.accuracy * 100)}% (${s.stats.evaluated} calls)`}</td>
        <td className="p-2">{s.kind === "youtube" ? <span className="text-xs text-muted-foreground">YouTube controls below</span> : s.kind === "existing" ? <span className="text-xs text-muted-foreground">Managed by its existing job</span> : <button disabled={busy} onClick={() => void act(async () => { await request(`/api/input-sources/research/sources/${s.id}`, jsonBody({ enabled: !s.enabled }, "PATCH")); })} className="underline">{s.enabled ? "Pause" : "Enable"}</button>}</td>
      </tr>)}</tbody></table></div>
    <p className="text-xs text-muted-foreground">Supplied means included in a persisted analyst report’s inputs. Cited means its source ID or URL appeared in the output. Neither establishes that the source caused a verdict change. Accuracy scores explicit price-direction calls at their stated horizon; unmeasurable claims remain unscored.</p>
    {selectedSource?.kind === "manual" && <form className="space-y-2" onSubmit={e => { e.preventDefault(); void act(async () => {
      await request("/api/input-sources/research/documents", jsonBody({ source_id: selectedSource.id, title, url, body: document }));
      setDocument(""); setTitle(""); setMessage("Document queued for the shared research fleet.");
    }); }}>
      <h3 className="font-medium">Add research from {selectedSource.name}</h3>
      <input aria-label="Document title" placeholder="Document title" value={title} onChange={e => setTitle(e.target.value)} required className="w-full rounded border border-border bg-background p-2" />
      <input aria-label="Original source URL" placeholder="Original URL (optional)" value={url} onChange={e => setUrl(e.target.value)} className="w-full rounded border border-border bg-background p-2" />
      <textarea aria-label="Research text" value={document} onChange={e => setDocument(e.target.value)} required minLength={100} maxLength={150000} rows={6} placeholder="Paste the research you have access to" className="w-full rounded border border-border bg-background p-2" />
      <button disabled={busy} className="rounded bg-primary text-primary-foreground px-3 py-2">Queue document</button>
    </form>}
    {selected !== null && <div className="space-y-3"><h3 className="font-medium">{selectedSource?.name}: recent items and review history</h3>{items.map(item => <details key={item.id} className="rounded border border-border p-3">
      <summary className="cursor-pointer">{item.title} <span className="text-xs text-muted-foreground">— {item.status}</span></summary>
      {/^https?:\/\//i.test(item.url) && <a href={item.url} target="_blank" rel="noreferrer" className="text-sm underline">Original source</a>}
      {item.summary && <p className="text-sm mt-2">{item.summary}</p>}
      {item.error && <p className="text-sm text-red-400 mt-2">{item.error}</p>}
      {item.status === "failed" && <button onClick={() => void act(async () => { await request(`/api/input-sources/research/items/${item.id}/retry`, { method: "POST" }); setMessage("Retry queued within the daily budget."); })} className="text-sm underline">Retry</button>}
      <ul className="space-y-2 mt-3">{item.reviews.map(r => <li key={r.ticker} className="text-sm"><strong>{r.ticker} · {r.state}</strong> — {r.reason}{r.result.rationale && <p className="text-muted-foreground">Review: {r.result.rationale} {r.result.action || ""}</p>}</li>)}</ul>
      <ul className="space-y-2 mt-3">{item.claims.map(c => <li key={c.id} className="text-sm">{c.ticker && <strong>{c.ticker}: </strong>}{c.statement}<p className="text-xs text-muted-foreground">{c.outcome ? `${c.outcome.verdict}${c.outcome.excess_return_pct !== undefined ? ` · vs SPY: ${c.outcome.excess_return_pct.toFixed(1)} percentage points` : ""}${c.outcome.reason ? ` · ${c.outcome.reason}` : ""}` : c.due_at ? `Evaluation due ${c.due_at.slice(0, 10)}` : "No measurable horizon; retained for qualitative review"}</p></li>)}</ul>
    </details>)}</div>}
  </section>;
}
