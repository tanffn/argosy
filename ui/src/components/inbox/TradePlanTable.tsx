"use client";

/**
 * TradePlanTable — ONE overview table for every open buy/sell decision:
 * current state | after changes | why. A pure projection of the server's
 * ``trade_plan`` block (every number derives from the latest snapshot +
 * the open proposals); the detail cards below stay the zoom-in surface.
 */

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { TradePlanDTO, TradePlanLineDTO } from "@/lib/api";

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

export function TradePlanTable({ plan }: { plan: TradePlanDTO }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-base">How your portfolio changes</CardTitle>
        <CardDescription>
          {usd(plan.totals.sells_usd)} of sells · {usd(plan.totals.buys_usd)} of buys ·{" "}
          {usd(plan.totals.net_to_cash_usd)} net to cash — as of {plan.as_of}. Decide each
          line on its card below.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs text-muted-foreground text-left">
              <tr className="border-b border-border/60">
                <th className="py-1.5 pr-3">Position</th>
                <th className="py-1.5 pr-3">Change</th>
                <th className="py-1.5 pr-3 whitespace-nowrap">Now</th>
                <th className="py-1.5 pr-3 whitespace-nowrap">After</th>
                <th className="py-1.5">Why</th>
              </tr>
            </thead>
            <tbody>
              {plan.lines.map((l) => (
                <tr key={l.item_id} className="border-b border-border/30 align-top">
                  <td className="py-2 pr-3 font-medium whitespace-nowrap">{l.label}</td>
                  <td className="py-2 pr-3 whitespace-nowrap">{actionBadge(l)}</td>
                  <td className="py-2 pr-3 font-mono text-xs whitespace-nowrap">
                    {stateCell(l.current_usd, l.current_pct)}
                  </td>
                  <td className="py-2 pr-3 font-mono text-xs whitespace-nowrap">
                    {stateCell(l.after_usd, l.after_pct)}
                  </td>
                  <td className="py-2 text-xs text-muted-foreground min-w-[16rem]">
                    {l.why}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
