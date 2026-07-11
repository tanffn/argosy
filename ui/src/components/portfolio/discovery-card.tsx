"use client";

import { useEffect, useState } from "react";

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
  type DiscoveryCandidateDTO,
  type DiscoveryDTO,
} from "@/lib/api";

function convictionTone(c: string): "success" | "secondary" | "outline" {
  if (c === "HIGH") return "success";
  if (c === "MED") return "secondary";
  return "outline";
}

function verdictTone(v: string): "success" | "secondary" | "destructive" {
  if (v === "BUY") return "success";
  if (v === "WATCH") return "secondary";
  return "destructive";
}

function fmtWhen(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function plainStatus(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function CandidateDetails({
  candidate,
}: {
  candidate: DiscoveryCandidateDTO | undefined;
}) {
  if (!candidate) return null;
  const proposal = candidate.latest_trade_proposal;
  const path = [
    candidate.source_labels.length > 0
      ? candidate.source_labels.join(" + ")
      : "Persisted scan",
    candidate.estimator
      ? `Estimator ${candidate.estimator.conviction} ${
          candidate.estimator.go ? "go" : "no-go"
        }`
      : null,
    candidate.fleet
      ? `Research ${candidate.fleet.verdict} / asymmetry ${candidate.fleet.conviction}`
      : null,
    proposal
      ? `Trade ${proposal.action.toUpperCase()} / confidence ${
          proposal.confidence ?? "not recorded"
        }`
      : null,
  ].filter(Boolean);

  return (
    <div className="mt-2 space-y-2 border-t border-border/50 pt-2 text-xs">
      <div>
        <span className="font-medium text-foreground">Source path: </span>
        {path.join(" → ")}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span>
          {plainStatus(candidate.status)}
          {candidate.rank !== null ? ` · rank ${candidate.rank}` : ""}
          {` · radar score ${candidate.radar_score.toFixed(1)}`}
        </span>
        {candidate.estimator && (
          <Badge
            variant={convictionTone(candidate.estimator.conviction)}
            className="text-[10px]"
          >
            Estimator: {candidate.estimator.conviction}
          </Badge>
        )}
        {proposal?.confidence && (
          <Badge variant="secondary" className="text-[10px]">
            Trade confidence: {proposal.confidence}
          </Badge>
        )}
      </div>
      {candidate.quarantine_reason && (
        <div>Filter reason: {plainStatus(candidate.quarantine_reason)}</div>
      )}
      {proposal && (
        <div>
          Proposal status: {plainStatus(proposal.status)}
          {proposal.decision_run_id !== null
            ? ` · decision run #${proposal.decision_run_id}`
            : ""}
          {` · ${fmtWhen(proposal.created_at)}`}
        </div>
      )}
    </div>
  );
}

/**
 * /proposals tile: the combined high-potential DISCOVERY surface — fleet-graded
 * picks (radar → cheap estimator triage → Opus fleet grade) plus the estimator
 * shortlist. Conviction/verdict only (no dollar sizing). The cached highlights
 * load instantly; "Refresh" runs the funnel (smart — only new/changed names are
 * re-researched). Click a pick to expand its thesis.
 *
 * These are NOT recommendations: high-risk single names; pair with a stop-loss.
 */
export function DiscoveryCard() {
  const [data, setData] = useState<DiscoveryDTO | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    api
      .portfolioDiscovery()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const refresh = () => {
    setLoading(true);
    setError(null);
    api
      .portfolioDiscoveryRefresh(false)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };

  const picks = data?.picks ?? [];
  const watch = (data?.estimated ?? []).filter(
    (e) => e.go && !picks.some((p) => p.ticker === e.ticker),
  );
  const candidates = data?.candidates ?? [];
  const candidateByTicker = new Map(candidates.map((row) => [row.ticker, row]));
  const surfacedTickers = new Set([
    ...picks.map((pick) => pick.ticker),
    ...watch.map((estimate) => estimate.ticker),
  ]);
  const otherCandidates = candidates.filter(
    (candidate) => !surfacedTickers.has(candidate.ticker),
  );
  const flow = data
    ? [
        ["Tracked", data.stages.tracked],
        ["Active after filters", data.stages.active],
        ["Estimated", data.stages.estimated],
        ["Estimator go", data.stages.estimator_go],
        ["Fleet graded", data.stages.fleet_graded],
        ["Research BUY", data.stages.fleet_buy],
        ["Trade proposals", data.stages.open_trade_proposals],
      ]
    : [];

  return (
    <Card className="border-warning/30">
      <CardHeader>
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <CardTitle className="text-base font-mono">
              High-potential discovery
            </CardTitle>
            <CardDescription className="mt-1">
              Persisted growth discovery (sources → estimator → research fleet →
              trade proposal). Research asymmetry and trade confidence are shown
              as separate stages.
              <span className="block mt-0.5 text-[11px]">
                Last refreshed: {fmtWhen(data?.last_refreshed_at ?? null)}
              </span>
            </CardDescription>
            {data && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {data.sources.length > 0 ? (
                  data.sources.map((source) => (
                    <Badge
                      key={source.key}
                      variant="outline"
                      className="text-[10px]"
                      title={`${source.label}: ${source.tracked_count} tracked`}
                    >
                      {source.label} · {source.active_count} active
                      {source.quarantined_count > 0
                        ? ` · ${source.quarantined_count} filtered`
                        : ""}
                      {source.dropped_stale_count > 0
                        ? ` · ${source.dropped_stale_count} stale`
                        : ""}
                    </Badge>
                  ))
                ) : (
                  <span className="text-[11px] text-muted-foreground">
                    No enabled discovery sources.
                  </span>
                )}
              </div>
            )}
          </div>
          <Button onClick={refresh} disabled={loading} size="sm" variant="outline">
            {loading ? "Refreshing…" : "Refresh"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {error && (
          <div className="text-xs text-destructive">Discovery failed: {error}</div>
        )}
        {data && picks.length === 0 && watch.length === 0 && !error && (
          <div className="text-xs text-muted-foreground">
            No graded picks yet. Click &ldquo;Refresh&rdquo; to run the discovery
            funnel (sources → triage → fleet grade).
          </div>
        )}

        {data && (
          <div className="rounded-md border border-border/60 bg-muted/20 p-2">
            <div className="flex flex-wrap items-center gap-1 text-[10px]">
              {flow.map(([label, count], index) => (
                <div key={label} className="contents">
                  {index > 0 && <span className="text-muted-foreground">→</span>}
                  <span className="rounded border border-border/60 px-1.5 py-1">
                    <span className="font-medium">{label}</span>{" "}
                    <span className="font-mono">{count}</span>
                  </span>
                </div>
              ))}
            </div>
            <div className="mt-1.5 flex flex-wrap gap-3 text-[10px] text-muted-foreground">
              <span>Quarantined: {data.stages.quarantined}</span>
              <span>Stale/dropped: {data.stages.dropped_stale}</span>
            </div>
          </div>
        )}

        {picks.map((p) => (
          <button
            key={p.ticker}
            type="button"
            onClick={() => setOpen(open === p.ticker ? null : p.ticker)}
            className="w-full text-left rounded-md border border-border bg-secondary/30 px-3 py-2 text-xs hover:bg-secondary/50"
            aria-expanded={open === p.ticker}
          >
            <div className="flex items-center gap-2 flex-wrap font-mono">
              <span className="font-semibold text-sm">{p.ticker}</span>
              <Badge variant={verdictTone(p.verdict)}>{p.verdict}</Badge>
              <Badge variant={convictionTone(p.conviction)} className="text-[10px]">
                Research asymmetry: {p.conviction}
              </Badge>
              {candidateByTicker.get(p.ticker)?.estimator && (
                <Badge
                  variant={convictionTone(
                    candidateByTicker.get(p.ticker)!.estimator!.conviction,
                  )}
                  className="text-[10px]"
                >
                  Estimator:{" "}
                  {candidateByTicker.get(p.ticker)!.estimator!.conviction}
                </Badge>
              )}
              <span className="ml-auto text-muted-foreground">
                {open === p.ticker ? "▾" : "▸"} rationale
              </span>
            </div>
            {open === p.ticker && (
              <div className="mt-2 whitespace-pre-wrap text-muted-foreground">
                {p.thesis_md}
                {p.cites.length > 0 && (
                  <div className="mt-1 text-[10px]">
                    research citations: {p.cites.join(", ")}
                  </div>
                )}
                <CandidateDetails candidate={candidateByTicker.get(p.ticker)} />
              </div>
            )}
          </button>
        ))}

        {watch.length > 0 && (
          <div className="pt-1">
            <div className="text-[11px] font-semibold text-muted-foreground">
              On the radar (estimator go, not yet fleet-graded)
            </div>
            {watch.map((e) => (
              <button
                key={e.ticker}
                type="button"
                onClick={() => setOpen(open === e.ticker ? null : e.ticker)}
                className="mt-1 w-full rounded-md border border-border/60 px-3 py-1.5 text-left text-xs font-mono hover:bg-secondary/30"
                aria-expanded={open === e.ticker}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold">{e.ticker}</span>
                  <Badge
                    variant={convictionTone(e.conviction)}
                    className="text-[10px]"
                  >
                    Estimator: {e.conviction}
                  </Badge>
                  <span className="text-muted-foreground">
                    sentiment {e.sentiment >= 0 ? "+" : ""}
                    {e.sentiment.toFixed(2)}
                  </span>
                  <span className="text-muted-foreground">· {e.one_line}</span>
                  <span className="ml-auto text-muted-foreground">
                    {open === e.ticker ? "▾" : "▸"} provenance
                  </span>
                </div>
                {open === e.ticker && (
                  <CandidateDetails candidate={candidateByTicker.get(e.ticker)} />
                )}
              </button>
            ))}
          </div>
        )}

        {otherCandidates.length > 0 && (
          <div className="pt-1">
            <div className="text-[11px] font-semibold text-muted-foreground">
              Other tracked candidate provenance
            </div>
            {otherCandidates.map((candidate) => (
              <button
                key={candidate.ticker}
                type="button"
                onClick={() =>
                  setOpen(open === candidate.ticker ? null : candidate.ticker)
                }
                className="mt-1 w-full rounded-md border border-border/60 px-3 py-1.5 text-left text-xs hover:bg-secondary/30"
                aria-expanded={open === candidate.ticker}
              >
                <div className="flex flex-wrap items-center gap-2 font-mono">
                  <span className="font-semibold">{candidate.ticker}</span>
                  <Badge variant="outline" className="text-[10px]">
                    {plainStatus(candidate.status)}
                  </Badge>
                  {candidate.estimator && (
                    <Badge
                      variant={convictionTone(candidate.estimator.conviction)}
                      className="text-[10px]"
                    >
                      Estimator: {candidate.estimator.conviction}
                    </Badge>
                  )}
                  <span className="ml-auto text-muted-foreground">
                    {open === candidate.ticker ? "▾" : "▸"} provenance
                  </span>
                </div>
                {open === candidate.ticker && (
                  <CandidateDetails candidate={candidate} />
                )}
              </button>
            ))}
          </div>
        )}

        {data && (
          <div className="mt-2 text-[11px] text-muted-foreground">{data.note}</div>
        )}
      </CardContent>
    </Card>
  );
}
