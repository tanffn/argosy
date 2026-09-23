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
  type DiscoverySourceDTO,
  type HistoricalReplayDTO,
  type NewsCoverageDTO,
  type RecommendationScorecardDTO,
} from "@/lib/api";
import { VerdictProvenanceStrip } from "@/components/verdict-provenance";

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

function fmtRate(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

function fmtPnl(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

function SourceScorecardDetails({ source }: { source: DiscoverySourceDTO }) {
  const scorecard = source.scorecard;
  if (!scorecard) return null;
  const short = scorecard.horizons["30d"];
  const thesis = scorecard.horizons["180d"];
  return (
    <details className="mt-1 max-w-sm rounded border border-border/50 px-2 py-1 text-[10px] text-muted-foreground">
      <summary className="cursor-pointer select-none text-foreground/80">
        performance
      </summary>
      <div className="mt-1 space-y-0.5">
        <div>
          Aggregate: {scorecard.scored_outcomes} scored · win{" "}
          {fmtRate(scorecard.win_rate)} · avg PnL {fmtPnl(scorecard.avg_pnl_pct)}
        </div>
        <div>
          30d: {short.scored_outcomes} scored · win {fmtRate(short.win_rate)} ·
          avg PnL {fmtPnl(short.avg_pnl_pct)}
        </div>
        <div>
          180d: {thesis.scored_outcomes} scored · win {fmtRate(thesis.win_rate)}{" "}
          · avg PnL {fmtPnl(thesis.avg_pnl_pct)} · always-long{" "}
          {fmtRate(thesis.always_long_same_tickers_win_rate)}
        </div>
        {scorecard.kill_reason && (
          <div className="text-warning">{scorecard.kill_reason}</div>
        )}
      </div>
    </details>
  );
}

function RecommendationScorecard({ data }: { data: RecommendationScorecardDTO }) {
  const tactical = data.horizons["30d"];
  const thesis = data.horizons["180d"];
  const annual = data.horizons["365d"];
  const selfEvaluations = data.self_evaluations ?? [];
  const recent = data.recent.filter((row) => row.horizon_days === 180).slice(0, 8);
  const surfaced = (data.surfaced_order_sheets?.recent ?? [])
    .filter((row) => row.horizon_days === 180)
    .slice(0, 8);
  const surfacedThesis = data.surfaced_order_sheets?.horizons["180d"];
  const benchmark = data.benchmark;
  const shadow = data.shadow_order_sheets;
  return (
    <details className="rounded border border-border/60 px-3 py-2 text-xs">
      <summary className="cursor-pointer font-medium">
        Automatic self-evaluation vs S&amp;P 500 — recommendations bought or not
      </summary>
      <div className="mt-2 space-y-2 text-muted-foreground">
        {benchmark && (
          <div className="rounded border border-border/50 p-2">
            <div className="font-medium text-foreground/80">
              Did Argosy add value over {benchmark.symbol}?
            </div>
            <div>
              {benchmark.compared} calls compared · {benchmark.beats} beat · {benchmark.lags} lagged · {benchmark.ties} tied · beat rate {fmtRate(benchmark.beat_rate)}
              {benchmark.avg_excess_return_pct !== null
                ? ` · average excess ${benchmark.avg_excess_return_pct >= 0 ? "+" : ""}${benchmark.avg_excess_return_pct.toFixed(1)}pp`
                : ""}
            </div>
            <div className="text-[10px]">{benchmark.basis}</div>
            {shadow && shadow.evaluated > 0 && (
              <div className="mt-1 border-t border-border/40 pt-1">
                Authored shadow allocations: {shadow.evaluated} evaluated · {shadow.beats} beat · {shadow.lags} lagged.
                {shadow.recent[0]
                  ? ` Latest: $${shadow.recent[0].allocated_usd.toLocaleString()} across ${shadow.recent[0].line_count} buys, ${shadow.recent[0].weighted_excess_return_pct >= 0 ? "+" : ""}${shadow.recent[0].weighted_excess_return_pct.toFixed(1)}pp vs SPY.`
                  : ""}
                <div className="text-[10px]">{shadow.basis}</div>
              </div>
            )}
          </div>
        )}
        <div className="grid gap-1 sm:grid-cols-3">
          <div>
            30-day: {tactical.graded}/{tactical.scheduled} graded · {tactical.wins} wins · {tactical.misses} misses · win {fmtRate(tactical.win_rate)}
          </div>
          <div>
            6-month: {thesis.graded}/{thesis.scheduled} graded · {thesis.wins} wins · {thesis.misses} misses · win {fmtRate(thesis.win_rate)}
          </div>
          <div>
            1-year: {annual.graded}/{annual.scheduled} graded · {annual.wins} wins · {annual.misses} misses · win {fmtRate(annual.win_rate)}
          </div>
        </div>
        <div>
          Forecast coverage: {data.coverage.with_30d_forecast}/{data.coverage.actionable_verdicts} at 30 days · {data.coverage.with_180d_forecast}/{data.coverage.actionable_verdicts} at 6 months · {data.coverage.with_365d_forecast}/{data.coverage.actionable_verdicts} at 1 year
        </div>
        {(data.coverage.unscorable_evaluations ?? 0) > 0 && (
          <div className="text-warning">
            {data.coverage.unscorable_evaluations} historical evaluations lack usable price evidence. They are not counted as wins or losses; see the reports below.
          </div>
        )}
        {data.coverage.legacy_proposals_without_verdict_link > 0 && (
          <details className="rounded border border-border/50 p-2">
            <summary className="cursor-pointer">
              Historical proposal coverage: {data.coverage.legacy_proposals_with_all_clocks ?? 0}/{data.coverage.legacy_proposals_without_verdict_link} with 30/180/365-day clocks
            </summary>
            <p className="my-2">These proposals predate verdict links. Original dates and rationale are retained; no verdicts or historical prices were invented. Clocks are not completed evaluations.</p>
            {(data.coverage.legacy_proposals_missing_clocks ?? []).map((row) => (
              <div key={row.proposal_id} className="text-warning">{row.ticker}: missing {row.missing_horizons.join("/")} day clocks</div>
            ))}
            <table className="w-full text-left text-[11px]">
              <thead><tr><th>Original call</th><th>You did</th><th>Six-month review</th><th>Report</th></tr></thead>
              <tbody>
                {(data.legacy_proposal_evaluations ?? []).map((row) => (
                  <tr key={row.prediction_id} className="border-t border-border/30 align-top">
                    <td className="py-1 pr-2"><details><summary className="cursor-pointer">{row.ticker} · {row.recommendation} · {new Date(row.recommended_at).toLocaleDateString()}</summary>{row.expectation}</details></td>
                    <td className="pr-2">{plainStatus(row.disposition)}</td>
                    <td className="pr-2">{new Date(row.evaluation_date).toLocaleDateString()}</td>
                    <td>{row.report}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}
        {selfEvaluations.length > 0 && (
          <div className="overflow-x-auto rounded border border-border/50 p-2">
            <div className="mb-1 font-medium text-foreground/80">
              Automatic self-evaluation
            </div>
            <table className="w-full min-w-[860px] text-left text-[11px]">
              <thead>
                <tr>
                  <th className="pr-3">Ticker / call</th>
                  <th className="pr-3">Made</th>
                  <th className="pr-3">Evaluate</th>
                  <th className="pr-3">Target</th>
                  <th>Evaluation report</th>
                </tr>
              </thead>
              <tbody>
                {selfEvaluations.slice(0, 36).map((row) => (
                  <tr key={row.prediction_id} className="align-top border-t border-border/30">
                    <td className="py-1 pr-3">
                      <div className="font-medium text-foreground">
                        {row.ticker} · {row.recommendation}
                        {row.conviction ? ` / ${row.conviction}` : ""}
                      </div>
                      <div>{plainStatus(row.source)} · {plainStatus(row.disposition)}</div>
                    </td>
                    <td className="py-1 pr-3">
                      {new Date(row.recommended_at).toLocaleDateString()}
                      {row.entry_price !== null ? ` @ $${row.entry_price.toFixed(2)}` : ""}
                    </td>
                    <td className="py-1 pr-3">
                      {new Date(row.evaluation_date).toLocaleDateString()}
                      <div>{row.horizon_days}d</div>
                    </td>
                    <td className="max-w-[280px] py-1 pr-3">
                      <details>
                        <summary className="cursor-pointer">View target</summary>
                        <div className="mt-1 whitespace-pre-wrap">{row.target}</div>
                        {row.expectation && (
                          <div className="mt-1 whitespace-pre-wrap text-foreground/70">
                            Expectation: {row.expectation}
                          </div>
                        )}
                      </details>
                    </td>
                    <td className="max-w-[300px] py-1">
                      <span className={row.grade === "miss" || row.grade === "missed_opportunity" ? "text-warning" : ""}>
                        {row.report}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {surfacedThesis && (
          <div>
            Shown in trade plans, whether accepted or ignored: {surfacedThesis.graded}/{surfacedThesis.scheduled} six-month calls graded; {surfacedThesis.wins} wins; {surfacedThesis.misses} misses
          </div>
        )}
        {surfaced.length > 0 && (
          <div className="overflow-x-auto">
            <div className="mb-1 font-medium text-foreground/80">Trade-plan calls</div>
            <table className="w-full text-left text-[11px]">
              <thead><tr><th className="pr-3">Call</th><th className="pr-3">You did</th><th className="pr-3">Due</th><th>Result</th></tr></thead>
              <tbody>
                {surfaced.map((row) => (
                  <tr key={row.prediction_id}>
                    <td className="pr-3">{row.action} {row.ticker}</td>
                    <td className="pr-3">{plainStatus(row.disposition)}</td>
                    <td className="pr-3">{new Date(row.due_at).toLocaleDateString()}</td>
                    <td>{plainStatus(row.grade)}{row.ticker_move_pct !== null ? `; ${row.ticker_move_pct >= 0 ? "+" : ""}${row.ticker_move_pct.toFixed(1)}%` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {recent.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead><tr><th className="pr-3">Call</th><th className="pr-3">You did</th><th className="pr-3">Due</th><th>Result</th></tr></thead>
              <tbody>
                {recent.map((row) => (
                  <tr key={row.prediction_id}>
                    <td className="pr-3">{row.action} {row.ticker}</td>
                    <td className="pr-3">{plainStatus(row.disposition)}</td>
                    <td className="pr-3">{new Date(row.due_at).toLocaleDateString()}</td>
                    <td>{plainStatus(row.grade)}{row.ticker_move_pct !== null ? ` · ${row.ticker_move_pct >= 0 ? "+" : ""}${row.ticker_move_pct.toFixed(1)}%` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data.radar_opportunities && (
          <div>
            Radar opportunities at 6 months: {data.radar_opportunities.graded_180d}/{data.radar_opportunities.scheduled_180d} graded; {data.radar_opportunities.winners_180d} winners; {data.radar_opportunities.missed_winners_180d} were on radar but not recommended.
          </div>
        )}
        {data.coverage.eligible_radar_names !== undefined && (
          <div className={data.coverage.radar_clock_status === "partial" ? "text-warning" : ""}>
            Radar clock coverage: {data.coverage.radar_names_with_180d_clock ?? 0}/{data.coverage.eligible_radar_names} currently eligible names have a six-month outcome clock
            {data.coverage.radar_clock_coverage_pct !== null && data.coverage.radar_clock_coverage_pct !== undefined
              ? ` (${data.coverage.radar_clock_coverage_pct.toFixed(1)}%)`
              : ""}.
            {data.coverage.radar_clock_status === "partial" && (data.coverage.radar_names_missing_180d_clock?.length ?? 0) > 0
              ? ` Missing: ${data.coverage.radar_names_missing_180d_clock!.join(", ")}.`
              : ""}
          </div>
        )}
        {data.coverage.universe_recall_status !== "available" && (
          <div className="text-warning">Full-market recall: {data.coverage.universe_recall_reason}</div>
        )}
      </div>
    </details>
  );
}

function ReplayLab({ data }: { data: HistoricalReplayDTO["lab"] }) {
  if (!data) return null;
  const pct = (value: number | null) => value == null ? "unknown" : `${value.toFixed(1)}%`;
  return (
    <details className="mt-2 rounded border border-border/60 p-2">
      <summary className="cursor-pointer font-medium">Decision lab — {data.status.replaceAll("_", " ")}</summary>
      <div className="mt-2 space-y-2">
        <div>Run {data.run_id}. Synthetic controls: {data.controls_passed ? "passed" : "not passed; historical interpretation blocked"}.</div>
        <div>Rechecked against current integrity rules; original decisions and review records remain unchanged.</div>
        <div>6 / 12 / 24-month market comparisons are gross diagnostics, not after-tax trade returns.</div>
        {data.cases.map((row) => (
          <details key={row.case_id} className="border-t border-border/30 pt-1">
            <summary className="cursor-pointer">{row.case_id} ({row.synthetic ? "fictional control" : "historical"}) — {row.action ?? "not run"} / {row.confidence ?? "unknown"}{!row.qualified && " — excluded"}</summary>
            {row.exclusion_reason && <div className="text-warning">{row.exclusion_reason}</div>}
            {row.review_violations?.map((value, i) => <div key={`violation-${i}`} className="text-warning">Review finding: {value}</div>)}
            {row.review_warnings?.map((value, i) => <div key={`warning-${i}`}>Review caveat: {value}</div>)}
            {row.grading_mismatches?.map((value, i) => <div key={`grading-${i}`} className="text-warning">Scoring mismatch ({value.field}): reference {JSON.stringify(value.expected)}, grader {JSON.stringify(value.actual)}.</div>)}
            {row.rationale && <div className="whitespace-pre-wrap">{row.rationale}</div>}
            {row.falsifiers?.map((value, i) => <div key={i}>Falsifier: {value}</div>)}
            {row.market_horizons?.map((h) => <div key={h.months}>{h.months} months: {h.status === "available" ? `asset ${pct(h.subject_return_pct)}; S&P 500 proxy ${pct(h.benchmark_return_pct)}; difference ${h.excess_return_pp?.toFixed(1)} percentage points` : h.status.replaceAll("_", " ")}</div>)}
            {!row.synthetic && !row.market_horizons && <div>Fixed-horizon market evidence not available yet.</div>}
          </details>
        ))}
        {data.outcome_errors?.map((text) => <div key={text} className="text-warning">{text}</div>)}
        {data.limitations.map((text) => <div key={text} className="text-[10px] text-muted-foreground">{text}</div>)}
      </div>
    </details>
  );
}

export function HistoricalReplay({ data }: { data: HistoricalReplayDTO }) {
  if (data.status !== "scored" || !data.raw_direction || !data.reviewer_certified) {
    return (
      <details className="rounded border border-border/60 px-3 py-2 text-xs">
        <summary className="cursor-pointer font-medium">Historical replay (anti-hindsight)</summary>
        <div className="mt-2 text-muted-foreground">{data.message}</div>
        <ReplayLab data={data.lab} />
      </details>
    );
  }
  const raw = data.raw_direction;
  const certified = data.reviewer_certified;
  return (
    <details className="rounded border border-border/60 px-3 py-2 text-xs">
      <summary className="cursor-pointer font-medium">
        Replay diagnostics -- {certified.correct}/{certified.total} qualified class matches
      </summary>
      <div className="mt-2 space-y-2 text-muted-foreground">
        <div className="rounded border border-border/50 p-2">
          <div className="font-medium text-foreground/80">Time-machine test, separate from live outcomes</div>
          <div>
            Qualified class matches: {certified.correct}/{certified.total} ({fmtRate(certified.rate)}) | raw direction: {raw.correct}/{raw.total} ({fmtRate(raw.rate)})
          </div>
          {data.evidence_counts && <div>{data.evidence_counts.synthetic} fictional controls; {data.evidence_counts.historical} historical cases. These are not live investment wins.</div>}
          <div>
            Coverage: {data.coverage.executed ?? 0}/{data.coverage.packets} packets replayed | {data.coverage.missing_receipt} missing / {data.coverage.invalid_receipt ?? 0} invalid sourcing receipts | {data.coverage.temporal_disqualified} rejected for hindsight risk
          </div>
          {certified.disqualified > 0 && (
            <div className="text-warning">
              {certified.disqualified} replay result{certified.disqualified === 1 ? " was" : "s were"} excluded: incomplete or failed integrity/review checks.
            </div>
          )}
          <div className="text-[10px]">{data.message}</div>
          <ReplayLab data={data.lab} />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[700px] text-left text-[11px]">
            <thead><tr><th className="pr-3">Frozen case</th><th className="pr-3">Expected</th><th className="pr-3">Argosy</th><th className="pr-3">Direction</th><th>Independent review</th></tr></thead>
            <tbody>
              {data.cases.map((row) => (
                <tr key={row.case_id} className="align-top border-t border-border/30">
                  <td className="py-1 pr-3 font-medium text-foreground">{row.case_id}</td>
                  <td className="py-1 pr-3 uppercase">{row.expected_actions.join(" / ") || "--"}</td>
                  <td className="py-1 pr-3 uppercase">{row.action} / {row.confidence}</td>
                  <td className={row.in_expected_class ? "py-1 pr-3 text-success" : "py-1 pr-3 text-warning"}>
                    {row.in_expected_class ? "correct" : "miss"}
                  </td>
                  <td className={row.reviewer_qualified ? "py-1" : "py-1 text-warning"}>
                    {row.reviewer_qualified ? "qualified" : "excluded -- see evidence"}
                    <details className="mt-0.5 text-muted-foreground">
                      <summary className="cursor-pointer">Decision evidence</summary>
                      <div className="max-w-xl space-y-1 whitespace-pre-wrap pt-1">
                        {row.rationale_summary && <div><span className="font-medium text-foreground/80">Why:</span> {row.rationale_summary}</div>}
                        {row.size !== null && <div><span className="font-medium text-foreground/80">Size:</span> {row.size.toLocaleString()} {row.size_units ?? ""}</div>}
                        {row.falsifiers.length > 0 && <div><span className="font-medium text-foreground/80">Falsifiers:</span> {row.falsifiers.join("; ")}</div>}
                        {row.next_validation_point && <div><span className="font-medium text-foreground/80">Next check:</span> {row.next_validation_point}</div>}
                        {row.rerating_horizon && <div><span className="font-medium text-foreground/80">Horizon:</span> {row.rerating_horizon}</div>}
                        {!row.reviewer_qualified && row.review_violations[0] && <div className="text-warning"><span className="font-medium">Excluded because:</span> {row.review_violations[0]}</div>}
                        {row.review_warnings?.map((value, i) => <div key={i}>Review caveat: {value}</div>)}
                      </div>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </details>
  );
}

function NewsCoverage({ data }: { data: NewsCoverageDTO }) {
  const s = data.summary;
  const disputed = data.holdings.filter((row) => row.latest_review_outcome === "held_unverified");
  return (
    <details className="rounded border border-border/60 px-3 py-2 text-xs">
      <summary className="cursor-pointer font-medium">News and earnings coverage</summary>
      <div className="mt-2 space-y-1 text-muted-foreground">
        <div>
          Recent news: {s.with_recent_news}/{s.held_single_stocks} held stocks · earnings-related signals: {s.with_earnings_signal}/{s.held_single_stocks} · fresh verdict reviews: {s.with_recent_review}/{s.held_single_stocks}
        </div>
        <div className={s.with_calendar_check < s.held_single_stocks ? "text-warning" : ""}>
          Earnings calendar checked: {s.with_calendar_check}/{s.held_single_stocks} held stocks · provider errors: {s.calendar_check_errors} · reported events awaiting a newer verdict: {s.recent_events_awaiting_review}
        </div>
        <div className={s.with_primary_filing_check < s.held_single_stocks ? "text-warning" : ""}>
          Primary SEC evidence: {s.with_primary_filing_evidence}/{s.held_single_stocks} held stocks · checks completed: {s.with_primary_filing_check}/{s.held_single_stocks} · provider errors: {s.primary_filing_errors} · genuinely recent filings: {s.with_recent_primary_filing}
        </div>
        <div>
          Jobs: earnings calendar {data.jobs.earnings_calendar_daily?.status ?? "missing"} · SEC filings {data.jobs.sec_earnings_daily?.status ?? "missing"} · news {data.jobs.news_daily?.status ?? "missing"} · holdings review {data.jobs.holdings_review?.status ?? "missing"}
        </div>
        {!s.full_earnings_call_coverage && (
          <div className="text-warning">Not full call coverage: calendar and primary SEC filing receipts are recorded, but issuer call transcripts are not connected yet.</div>
        )}
        {data.holdings.filter((row) => row.earnings_review_gap).map((row) => (
          <div key={`earnings-${row.ticker}`} className="text-warning">
            {row.ticker}: {row.earnings_review_gap}
          </div>
        ))}
        {disputed.map((row) => (
          <div key={row.ticker} className="text-warning">
            <div>{row.ticker}: {row.latest_review_verdict} disputed by verification — sent to unified reconciliation.</div>
            {row.latest_review_reason && <div className="ml-3 text-muted-foreground">Why: {row.latest_review_reason}</div>}
            {row.verification_verdict && <div className="ml-3">Second reviewer: {row.verification_verdict}{row.verification_reason ? ` — ${row.verification_reason}` : ""}</div>}
            {row.execution_blocker && <div className="ml-3">Blocked: {row.execution_blocker}</div>}
          </div>
        ))}
      </div>
    </details>
  );
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
  const [recommendations, setRecommendations] = useState<RecommendationScorecardDTO | null>(null);
  const [historicalReplay, setHistoricalReplay] = useState<HistoricalReplayDTO | null>(null);
  const [newsCoverage, setNewsCoverage] = useState<NewsCoverageDTO | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    api
      .portfolioDiscovery()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
    if (typeof api.recommendationScorecard === "function") {
      api.recommendationScorecard("ariel").then(setRecommendations).catch(() => null);
    }
    if (typeof api.newsCoverage === "function") {
      api.newsCoverage("ariel").then(setNewsCoverage).catch(() => null);
    }
    if (typeof api.historicalReplay === "function") {
      api.historicalReplay().then(setHistoricalReplay).catch(() => null);
    }
    const refreshRecommendations = () => {
      if (typeof api.recommendationScorecard === "function") {
        api.recommendationScorecard("ariel").then(setRecommendations).catch(() => null);
      }
    };
    const interval = window.setInterval(refreshRecommendations, 60_000);
    window.addEventListener("focus", refreshRecommendations);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", refreshRecommendations);
    };
  }, []);

  const refresh = () => {
    setLoading(true);
    setError(null);
    Promise.all([
      api.portfolioDiscoveryRefresh(false),
      typeof api.recommendationScorecard === "function"
        ? api.recommendationScorecard("ariel")
        : Promise.resolve(null),
    ])
      .then(([discovery, scorecard]) => {
        setData(discovery);
        if (scorecard) setRecommendations(scorecard);
      })
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
                    <div key={source.key}>
                      <Badge
                        variant="outline"
                        className="text-[10px]"
                        title={`${source.label}: ${source.tracked_count} tracked`}
                      >
                        {source.label} · {source.active_count} active
                        {source.scorecard?.calibration.startsWith(
                          "uncalibrated",
                        )
                          ? ` · beta ${source.scorecard.scored_outcomes} scored`
                          : source.scorecard
                            ? ` · ${source.scorecard.scored_outcomes} scored`
                            : ""}
                        {source.scorecard &&
                        !source.scorecard.funnel_context_enabled
                          ? " · funnel voice paused"
                          : ""}
                        {source.quarantined_count > 0
                          ? ` · ${source.quarantined_count} filtered`
                          : ""}
                        {source.dropped_stale_count > 0
                          ? ` · ${source.dropped_stale_count} stale`
                          : ""}
                      </Badge>
                      <SourceScorecardDetails source={source} />
                    </div>
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
        {recommendations && <RecommendationScorecard data={recommendations} />}
        {historicalReplay && <HistoricalReplay data={historicalReplay} />}
        {newsCoverage && <NewsCoverage data={newsCoverage} />}
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
              <div className="mt-2 space-y-2 whitespace-pre-wrap text-muted-foreground">
                <VerdictProvenanceStrip
                  provenance={{
                    falsifier_state: p.falsifier_state ?? "none_recorded",
                    falsifiers: p.falsifiers ?? [],
                    next_validation: p.next_validation ?? null,
                    last_fleet_check_at: p.last_fleet_check_at ?? null,
                  }}
                />
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
