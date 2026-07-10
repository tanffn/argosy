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
      <td className="py-1.5 text-xs text-muted-foreground min-w-[16rem]">{line.why}</td>
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
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">How your portfolio changes</CardTitle>
        <CardDescription>
          {usd(plan.totals.sells_usd)} of sells · {usd(plan.totals.net_to_cash_usd)} net
          proceeds — as of {plan.as_of}. Decide each line on its card below.
        </CardDescription>
      </CardHeader>
      <CardContent>
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
