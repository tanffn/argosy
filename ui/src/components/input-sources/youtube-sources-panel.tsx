"use client";

import { AlertCircle, CheckCircle2, Loader2, Play, Plus, RefreshCw, Youtube } from "lucide-react";
import { FormEvent, useCallback, useEffect, useState, type ReactNode } from "react";

interface SourceStats {
  videos_ingested: number;
  tickers_collected: number;
  tickers: string[];
  claims_captured: number;
  claims_open: number;
  claims_evaluated: number;
  accuracy: number | null;
  last_analyzed_at: string | null;
}

interface YouTubeSource {
  id: number;
  youtube_channel_id: string;
  channel_name: string;
  channel_url: string;
  enabled: boolean;
  last_seen_video_id: string | null;
  last_polled_at: string | null;
  last_error: string | null;
  stats: SourceStats;
}

interface SourcesResponse {
  sources: YouTubeSource[];
  totals: {
    sources: number;
    enabled: number;
    videos_ingested: number;
    tickers_collected: number;
    claims_captured: number;
    claims_evaluated: number;
  };
}

const empty: SourcesResponse = {
  sources: [],
  totals: { sources: 0, enabled: 0, videos_ingested: 0, tickers_collected: 0, claims_captured: 0, claims_evaluated: 0 },
};

export function YouTubeSourcesPanel() {
  const [data, setData] = useState<SourcesResponse>(empty);
  const [reference, setReference] = useState("");
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    const response = await fetch("/api/input-sources/youtube?user_id=ariel", { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load YouTube sources");
    setData((await response.json()) as SourcesResponse);
  }, []);

  useEffect(() => {
    let active = true;
    fetch("/api/input-sources/youtube?user_id=ariel", { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error("Could not load YouTube sources");
        return response.json() as Promise<SourcesResponse>;
      })
      .then((result) => { if (active) setData(result); })
      .catch((error: Error) => { if (active) setMessage(error.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [load]);

  async function addSource(event: FormEvent) {
    event.preventDefault();
    if (!reference.trim()) return;
    setWorking("add"); setMessage(null);
    try {
      const response = await fetch("/api/input-sources/youtube", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reference: reference.trim(), user_id: "ariel" }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.detail ?? "Could not add this YouTube source");
      }
      setReference(""); setMessage("Source subscribed. New uploads will enter the daily discovery run.");
      await load();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not add source"); }
    finally { setWorking(null); }
  }

  async function toggle(source: YouTubeSource) {
    setWorking(`toggle-${source.id}`); setMessage(null);
    try {
      const response = await fetch(`/api/input-sources/youtube/${source.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !source.enabled, user_id: "ariel" }),
      });
      if (!response.ok) throw new Error("Could not update source");
      await load();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not update source"); }
    finally { setWorking(null); }
  }

  async function syncNow() {
    setWorking("sync"); setMessage("Checking enabled channels. New videos can take several minutes to analyze.");
    try {
      const response = await fetch("/api/jobs/youtube_subscriptions/run-now", { method: "POST" });
      if (!response.ok) throw new Error("Source sync failed");
      const result = await response.json();
      setMessage(`Shared research job ${result.job_run_id} started. Deferred videos stay queued within the daily budget.`);
      await load();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Source sync failed"); }
    finally { setWorking(null); }
  }

  return (
    <section className="space-y-5" aria-labelledby="youtube-sources-title">
      <div className="rounded-xl border border-border bg-card p-5">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="flex gap-3">
            <div className="rounded-lg bg-red-500/10 p-2 h-fit"><Youtube className="h-5 w-5 text-red-400" aria-hidden /></div>
            <div><h2 id="youtube-sources-title" className="text-lg font-semibold">YouTube research</h2>
              <p className="text-sm text-muted-foreground mt-1">Paste a channel or video URL. A video URL subscribes its publisher without replaying the channel backlog.</p></div>
          </div>
          <button type="button" onClick={syncNow} disabled={working !== null || data.totals.enabled === 0}
            className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm hover:bg-secondary disabled:opacity-50">
            {working === "sync" ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />} Sync now
          </button>
        </div>
        <form onSubmit={addSource} className="flex gap-2 mt-5">
          <label className="sr-only" htmlFor="youtube-reference">YouTube channel or video URL</label>
          <input id="youtube-reference" value={reference} onChange={(event) => setReference(event.target.value)}
            placeholder="https://youtube.com/@channel or video URL"
            className="flex-1 min-w-0 rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary" />
          <button disabled={working !== null || !reference.trim()} className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
            {working === "add" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />} Add source
          </button>
        </form>
        {message && <p role="status" className="text-sm text-muted-foreground mt-3">{message}</p>}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
        <Stat label="Sources" value={`${data.totals.enabled}/${data.totals.sources}`} />
        <Stat label="Videos ingested" value={data.totals.videos_ingested} />
        <Stat label="Tickers collected" value={data.totals.tickers_collected} />
        <Stat label="Claims captured" value={data.totals.claims_captured} />
        <Stat label="Claims evaluated" value={data.totals.claims_evaluated} />
        <Stat label="Daily check" value="14:00" />
      </div>

      {loading ? <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> Loading sources…</div> :
       data.sources.length === 0 ? <div className="rounded-xl border border-dashed border-border p-10 text-center text-muted-foreground">No YouTube sources yet. Add one above to start the research feed.</div> :
       <div className="space-y-3">{data.sources.map((source) => <SourceCard key={source.id} source={source} busy={working === `toggle-${source.id}`} onToggle={() => toggle(source)} />)}</div>}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return <div className="rounded-lg border border-border bg-card p-3"><p className="text-xs text-muted-foreground">{label}</p><p className="text-xl font-mono font-semibold mt-1">{value}</p></div>;
}

function SourceCard({ source, busy, onToggle }: { source: YouTubeSource; busy: boolean; onToggle: () => void }) {
  const stats = source.stats;
  return <article className="rounded-xl border border-border bg-card p-5">
    <div className="flex items-start justify-between gap-4">
      <div><a href={source.channel_url} target="_blank" rel="noreferrer" className="font-semibold hover:text-primary">{source.channel_name}</a>
        <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
          {source.last_error ? <AlertCircle className="h-3.5 w-3.5 text-destructive" /> : <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />}
          {source.last_error ? source.last_error : source.last_polled_at ? `Checked ${formatDate(source.last_polled_at)}` : "Ready for first check"}
        </div></div>
      <button type="button" role="switch" aria-checked={source.enabled} onClick={onToggle} disabled={busy}
        className={`rounded-full px-3 py-1 text-xs font-medium ${source.enabled ? "bg-emerald-500/15 text-emerald-300" : "bg-secondary text-muted-foreground"}`}>
        {busy ? "Saving…" : source.enabled ? "Enabled" : "Paused"}
      </button>
    </div>
    <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mt-5 text-sm">
      <Metric icon={<Play className="h-3.5 w-3.5" />} label="Videos" value={stats.videos_ingested} />
      <Metric label="Tickers" value={stats.tickers_collected} />
      <Metric label="Claims" value={stats.claims_captured} />
      <Metric label="Open calls" value={stats.claims_open} />
      <Metric label="Accuracy" value={stats.accuracy === null ? "Collecting" : `${Math.round(stats.accuracy * 100)}%`} />
    </div>
    {stats.tickers.length > 0 && <div className="flex flex-wrap gap-1.5 mt-4">{stats.tickers.map((ticker) => <span key={ticker} className="rounded bg-secondary px-2 py-0.5 text-xs font-mono">{ticker}</span>)}</div>}
  </article>;
}

function Metric({ label, value, icon }: { label: string; value: string | number; icon?: ReactNode }) {
  return <div><p className="flex items-center gap-1 text-xs text-muted-foreground">{icon}{label}</p><p className="font-mono mt-1">{value}</p></div>;
}

function formatDate(value: string) { return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); }
