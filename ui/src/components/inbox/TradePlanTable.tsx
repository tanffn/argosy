"use client";

/**
 * TradePlanTable — ONE overview table for every open buy/sell decision,
 * grouped BY SLEEVE:
 *   + sleeve header (current → after vs its plan target)
 *     -- movement lines (current | after | why)
 * A pure projection of the server's ``trade_plan`` block (every number
 * derives from the latest snapshot + open proposals + the plan's class
 * targets); the detail cards below stay the zoom-in surface.
 */

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { TradePlanDTO, TradePlanGroupDTO, TradePlanLineDTO } from "@/lib/api";

function usd(n: number): string {
  return n.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

function stateCell(usdV: number, pct: number | null): string {
  return pct === null ? usd(usdV) : `${usd(usdV)} (${pct.toFixed(1)}%)`;
}

function actionBadge(line: TradePlanLineDTO) {
  if (line.action === "sell")
    return <span className="text-error font-medium">Sell {usd(-line.delta_usd)}</span>;
  if (line.action === "buy")
    return <span className="text-success font-medium">Buy {usd(line.delta_usd)}</span>;
  return (
    <span className="text-muted-foreground font-medium">
      {line.delta_usd >= 0 ? "+" : ""}
      {usd(line.delta_usd)} net
    </span>
  );
}

function LineRow({ line }: { line: TradePlanLineDTO }) {
  const scenarios = line.outcome_scenarios ?? [];
  return (
    <tr className="border-b border-border/20 align-top">
      <td className="py-1.5 pr-3 pl-5 font-medium whitespace-nowrap">{line.label}</td>
      <td className="py-1.5 pr-3 whitespace-nowrap">{actionBadge(line)}</td>
      <td className="py-1.5 pr-3 font-mono text-xs whitespace-nowrap">
        {stateCell(line.current_usd, line.current_pct)}
      </td>
      <td className="py-1.5 pr-3 font-mono text-xs whitespace-nowrap">
        {stateCell(line.after_usd, line.after_pct)}
      </td>
      <td className="py-1.5 text-xs text-muted-foreground min-w-[16rem]">
        <div>{line.why}</div>
        {line.staged_execution && (
          <div className="mt-1 rounded border border-warning/40 bg-warning/5 p-2 text-foreground">
            <strong>Current clip only:</strong> execute by {line.staged_execution.execute_by};
            {` reassess ${line.staged_execution.next_review_date} before another sale. `}
            {line.staged_execution.tranche_reason}
            <div className="mt-1 text-muted-foreground">
              Next waypoint {line.staged_execution.next_waypoint_date}
              {line.staged_execution.next_waypoint_weight_pct != null
                ? ` at ≤${line.staged_execution.next_waypoint_weight_pct.toFixed(1)}% direct NVDA`
                : ""}
              {` · ${line.staged_execution.shares_to_sell_by_next_waypoint.toLocaleString()} total shares by the waypoint`}
              {line.staged_execution.tax_denominator_adjustment_shares > 0
                ? ` (${line.staged_execution.glide_base_shares_to_sell_by_next_waypoint.toLocaleString()} glide shares + ${line.staged_execution.tax_denominator_adjustment_shares.toLocaleString()} because tax shrinks the book)`
                : ""}
              {line.staged_execution.estimated_post_trade_direct_nvda_weight_pct != null
                ? ` · after this clip: ${line.staged_execution.estimated_post_trade_direct_nvda_weight_pct.toFixed(2)}% direct NVDA`
                : ""}
              {line.staged_execution.estimated_post_trade_effective_nvda_weight_pct != null
                ? ` / ${line.staged_execution.estimated_post_trade_effective_nvda_weight_pct.toFixed(2)}% effective look-through`
                : ""}
              {" · no automatic next tranche"}
            </div>
            {line.staged_execution.clips?.length > 1 && (
              <div className="mt-1 text-muted-foreground">
                Indicative path: {line.staged_execution.clips.map((clip) =>
                  `${clip.target_date}: ${clip.shares.toLocaleString()} sh${clip.executable_now ? " (current)" : " (reprice/review)"}`
                ).join(" · ")}
              </div>
            )}
          </div>
        )}
        {scenarios.length > 0 && (
          <details className="mt-1 rounded border border-border/60 p-2">
            <summary className="cursor-pointer font-medium text-foreground">
              Probability-aware sizing
              {line.probability_weighted_multiple != null
                ? ` · ${line.probability_weighted_multiple.toFixed(2)}× weighted case`
                : ""}
              {line.probability_confidence
                ? ` · ${line.probability_confidence.toLowerCase()} confidence`
                : ""}
            </summary>
            <div className="mt-1">
              {scenarios.map((scenario) => (
                <div key={`${scenario.label}:${scenario.terminal_multiple}`}>
                  <strong>{scenario.label}:</strong> {scenario.probability_pct.toFixed(0)}% at {scenario.terminal_multiple.toFixed(1)}× — {scenario.rationale}
                </div>
              ))}
              <div className="mt-1">
                Capital at risk: {line.capital_at_risk_pct?.toFixed(2) ?? "—"}% of book
                {line.expected_portfolio_contribution_pct != null
                  ? ` · probability-weighted contribution ${line.expected_portfolio_contribution_pct >= 0 ? "+" : ""}${line.expected_portfolio_contribution_pct.toFixed(2)}%`
                  : ""}
              </div>
              {line.scenario_terminal_date && (
                <div>
                  Through {line.scenario_terminal_date}:
                  {line.annualized_expected_return_pct != null
                    ? ` ${line.annualized_expected_return_pct.toFixed(1)}% annualized pre-tax`
                    : ""}
                  {line.after_tax_expected_multiple != null
                    ? ` · ${line.after_tax_expected_multiple.toFixed(2)}× after tax`
                    : ""}
                  {line.after_tax_annualized_expected_return_pct != null
                    ? ` (${line.after_tax_annualized_expected_return_pct.toFixed(1)}% annualized)`
                    : ""}
                </div>
              )}
              {(line.median_terminal_multiple != null || line.probability_of_loss_pct != null) && (
                <div>
                  Median {line.median_terminal_multiple?.toFixed(2) ?? "—"}×
                  {line.probability_of_loss_pct != null
                    ? ` · ${line.probability_of_loss_pct.toFixed(0)}% below cost`
                    : ""}
                  {line.probability_of_near_wipeout_pct != null
                    ? ` · ${line.probability_of_near_wipeout_pct.toFixed(0)}% near-wipeout`
                    : ""}
                </div>
              )}
              {line.probability_basis && (
                <div className="mt-1"><strong>Basis:</strong> {line.probability_basis}</div>
              )}
            </div>
          </details>
        )}
        {line.candidate_comparison_status === "missing" && (
          <div className="mt-1 rounded border border-error/40 p-2 text-error">
            Comparative selection rationale is missing from this older run. Argosy cannot prove why this name beat the other discovery finalists; rerun before approval.
          </div>
        )}
      </td>
    </tr>
  );
}

function GroupRows({ group }: { group: TradePlanGroupDTO }) {
  const target =
    group.target_pct !== null
      ? `plan target ${group.target_pct.toFixed(group.target_pct % 1 ? 2 : 0)}%${
          group.target_usd ? ` (${usd(group.target_usd)})` : ""
        }`
      : "";
  return (
    <>
      <tr className="border-b border-border/50 bg-muted/30 align-top">
        <td className="py-2 pr-3 font-semibold whitespace-nowrap">{group.label}</td>
        <td className="py-2 pr-3 text-xs text-muted-foreground whitespace-nowrap">{target}</td>
        <td className="py-2 pr-3 font-mono text-xs whitespace-nowrap">{usd(group.current_usd)}</td>
        <td className="py-2 pr-3 font-mono text-xs whitespace-nowrap">{usd(group.after_usd)}</td>
        <td className="py-2 text-xs text-muted-foreground min-w-[16rem]">{group.why}</td>
      </tr>
      {group.lines.map((l) => (
        <LineRow key={l.item_id} line={l} />
      ))}
    </>
  );
}

export function TradePlanTable({ plan }: { plan: TradePlanDTO }) {
  const groups = plan.groups ?? [];
  const finalistComparisons = plan.candidate_comparisons ?? [];
  const selectedFinalists = finalistComparisons.filter((row) => row.selection === "SELECTED");
  const review = plan.review_resolution;
  const resolvedObjections = review?.objections.filter(
    (row) => row.status === "resolved_by_re_review",
  ) ?? [];
  const advisoryObjections = review?.objections.filter(
    (row) => row.status === "advisory",
  ) ?? [];
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">How your portfolio changes</CardTitle>
        <CardDescription>
          {plan.source === "order_sheet" ? (
            <>
              {usd(plan.totals.buys_usd)} of buys
              {plan.totals.sells_usd > 0
                ? ` · ${usd(plan.totals.sells_usd)} of sells`
                : ""}
              {plan.new_cash_usd
                ? ` · funded by ${usd(plan.new_cash_usd)} new cash`
                : ""}
              {` — as of ${plan.as_of}.`}
            </>
          ) : (
            <>
              {usd(plan.totals.sells_usd)} of sells ·{" "}
              {usd(plan.totals.net_to_cash_usd)} net proceeds — as of {plan.as_of}.
              Decide each line on its card below.
            </>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {plan.approval_blocked && (
          <div className="mb-3 rounded border border-error/40 bg-error/5 p-2 text-xs text-error">
            This is the one current trade plan, but approval is blocked. Older
            suggestions remain hidden; repair or rerun this plan first.
          </div>
        )}
        {review && (
          <details className="mb-3 rounded border border-border/60 p-3 text-xs">
            <summary className="cursor-pointer font-medium text-foreground">
              {review.one_voice ? "Independent review reconciled" : "Independent review unresolved"}
              {` · ${review.reviewers_ran}/${review.reviewers_expected} reviewers · ${review.rounds} round${review.rounds === 1 ? "" : "s"}`}
            </summary>
            <div className="mt-2 space-y-2">
              <div>{review.summary}</div>
              {resolvedObjections.map((objection, index) => (
                <div key={`resolved:${objection.round}:${objection.lens}:${objection.ticker}:${index}`} className="rounded bg-muted/30 p-2">
                  <strong>{objection.ticker} · {objection.lens} resolved:</strong>{" "}
                  {objection.concern}
                  {objection.recommended_amount_usd != null
                    ? ` Reviewer recommended ${usd(objection.recommended_amount_usd)} instead of ${usd(objection.proposed_amount_usd)}.`
                    : ""}
                  {objection.recommended_ticker
                    ? ` Reviewer recommended ${objection.recommended_ticker}.`
                    : ""}
                </div>
              ))}
              {advisoryObjections.map((objection, index) => (
                <div key={`advisory:${objection.round}:${objection.lens}:${objection.ticker}:${index}`} className="text-muted-foreground">
                  <strong>{objection.ticker} · {objection.lens} note:</strong> {objection.concern}
                </div>
              ))}
            </div>
          </details>
        )}
        {finalistComparisons.length > 0 && (
          <details className="mb-3 rounded border border-border/60 p-3 text-xs">
            <summary className="cursor-pointer font-medium text-foreground">
              Discovery finalist decision — {selectedFinalists.length > 0
                ? `${selectedFinalists.map((row) => row.ticker).join(", ")} selected`
                : "no moonshot funded this run"}
            </summary>
            <div className="mt-2 space-y-2">
              {finalistComparisons.map((candidate) => (
                <div key={candidate.ticker} className="rounded bg-muted/30 p-2">
                  <div className="flex flex-wrap items-center gap-2 font-medium">
                    <span>{candidate.ticker}</span>
                    <span className={candidate.selection === "SELECTED" ? "text-success" : "text-muted-foreground"}>
                      {candidate.selection === "SELECTED" ? "Selected" : "Deferred"}
                    </span>
                    <span className="font-normal text-muted-foreground">
                      radar #{candidate.radar_rank ?? "—"} / {candidate.radar_score?.toFixed(1) ?? "—"}
                      {candidate.research_verdict ? ` · research ${candidate.research_verdict}/${candidate.research_conviction ?? "—"}` : ""}
                    </span>
                  </div>
                  <div className="mt-1"><strong>Why:</strong> {candidate.why}</div>
                  <div><strong>Advantage:</strong> {candidate.key_advantage}</div>
                  <div><strong>Risk:</strong> {candidate.key_risk}</div>
                  {candidate.recommended_position_usd != null && (
                    <div className="mt-1">
                      <strong>Warranted size:</strong> {usd(candidate.recommended_position_usd)}
                      {candidate.capital_at_risk_pct != null
                        ? ` (${candidate.capital_at_risk_pct.toFixed(2)}% of book at risk)`
                        : ""}
                      {candidate.probability_weighted_multiple != null
                        ? ` · ${candidate.probability_weighted_multiple.toFixed(2)}× probability-weighted`
                        : ""}
                      {candidate.expected_portfolio_contribution_pct != null
                        ? ` · ${candidate.expected_portfolio_contribution_pct >= 0 ? "+" : ""}${candidate.expected_portfolio_contribution_pct.toFixed(2)}% expected contribution`
                        : ""}
                    </div>
                  )}
                  {candidate.scenario_terminal_date && (
                    <div>
                      <strong>Dated return:</strong> through {candidate.scenario_terminal_date}
                      {candidate.annualized_expected_return_pct != null
                        ? ` · ${candidate.annualized_expected_return_pct.toFixed(1)}% annualized pre-tax`
                        : ""}
                      {candidate.after_tax_expected_multiple != null
                        ? ` · ${candidate.after_tax_expected_multiple.toFixed(2)}× after tax`
                        : ""}
                      {candidate.after_tax_annualized_expected_return_pct != null
                        ? ` (${candidate.after_tax_annualized_expected_return_pct.toFixed(1)}% annualized)`
                        : ""}
                    </div>
                  )}
                  {(candidate.median_terminal_multiple != null || candidate.probability_of_loss_pct != null) && (
                    <div>
                      <strong>Distribution:</strong>
                      {candidate.median_terminal_multiple != null ? ` median ${candidate.median_terminal_multiple.toFixed(2)}×` : ""}
                      {candidate.probability_of_loss_pct != null ? ` · ${candidate.probability_of_loss_pct.toFixed(0)}% below cost` : ""}
                      {candidate.probability_of_near_wipeout_pct != null ? ` · ${candidate.probability_of_near_wipeout_pct.toFixed(0)}% near-wipeout` : ""}
                    </div>
                  )}
                  {candidate.recommended_position_usd != null && candidate.recommended_position_usd > 0 && (
                    <div>
                      <strong>Why this size:</strong>{" "}
                      {candidate.smaller_position_usd != null && candidate.why_not_smaller
                        ? `${usd(candidate.smaller_position_usd)} was too small because ${candidate.why_not_smaller} `
                        : ""}
                      {candidate.larger_position_usd != null && candidate.why_not_larger
                        ? `${usd(candidate.larger_position_usd)} was too large because ${candidate.why_not_larger}`
                        : ""}
                    </div>
                  )}
                  {candidate.recommended_position_usd === 0 && candidate.larger_position_usd != null && candidate.why_not_larger && (
                    <div><strong>Why zero:</strong> Argosy considered {usd(candidate.larger_position_usd)} but rejected it because {candidate.why_not_larger}</div>
                  )}
                  {candidate.split_considered != null && candidate.split_why && (
                    <div><strong>Split:</strong> {candidate.split_considered ? "Considered" : "Not warranted"} — {candidate.split_why}</div>
                  )}
                  {(candidate.outcome_scenarios?.length ?? 0) > 0 && (
                    <details className="mt-1 rounded border border-border/50 p-2">
                      <summary className="cursor-pointer">
                        Equal-basis probability model · {(candidate.probability_confidence ?? "—").toLowerCase()} confidence
                      </summary>
                      <div className="mt-1">
                        {candidate.outcome_scenarios?.map((scenario) => (
                          <div key={`${candidate.ticker}:${scenario.label}:${scenario.terminal_multiple}`}>
                            <strong>{scenario.label}:</strong> {scenario.probability_pct.toFixed(0)}% at {scenario.terminal_multiple.toFixed(1)}× — {scenario.rationale}
                          </div>
                        ))}
                        {candidate.probability_basis && (
                          <div className="mt-1"><strong>Basis:</strong> {candidate.probability_basis}</div>
                        )}
                      </div>
                    </details>
                  )}
                </div>
              ))}
            </div>
          </details>
        )}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs text-muted-foreground text-left">
              <tr className="border-b border-border/60">
                <th className="py-1.5 pr-3">Sleeve / position</th>
                <th className="py-1.5 pr-3">Change</th>
                <th className="py-1.5 pr-3 whitespace-nowrap">Now</th>
                <th className="py-1.5 pr-3 whitespace-nowrap">After</th>
                <th className="py-1.5">Why</th>
              </tr>
            </thead>
            <tbody>
              {groups.length > 0
                ? groups.map((g) => <GroupRows key={g.label} group={g} />)
                : plan.lines.map((l) => <LineRow key={l.item_id} line={l} />)}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
