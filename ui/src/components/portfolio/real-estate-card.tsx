"use client";

import { Fragment, useEffect, useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type RealEstateEquityDTO } from "@/lib/api";

function fmtUsdK(k: number | null): string {
  if (k == null) return "—";
  if (Math.abs(k) >= 1000) return `$${(k / 1000).toFixed(2)}M`;
  return `$${k.toFixed(0)}K`;
}

function fmtLocal(v: number | null, ccy: string): string {
  if (v == null) return "—";
  const sym = ccy === "USD" ? "$" : ccy === "EUR" ? "€" : ccy === "NIS" ? "₪" : "";
  return `${sym}${Math.round(v).toLocaleString()}`;
}

/**
 * Real-estate net equity — the 4 properties (net of mortgage), as net-worth
 * context. Deliberately separate from the investable allocation/target: a
 * primary residence isn't investable capital.
 */
export function RealEstateCard({ userId = "ariel" }: { userId?: string }) {
  const [data, setData] = useState<RealEstateEquityDTO | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .portfolioRealEstate(userId)
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, [userId]);

  const [expanded, setExpanded] = useState<string | null>(null);

  if (error || !data || data.properties.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <CardTitle>Real estate</CardTitle>
          <span className="font-mono text-sm">{fmtUsdK(data.total_net_usd_k)} net</span>
        </div>
        <CardDescription>{data.note}</CardDescription>
      </CardHeader>
      <CardContent>
        <table className="w-full text-sm font-mono">
          <thead>
            <tr className="text-left text-xs text-muted-foreground border-b border-border">
              <th className="py-2">Property</th>
              <th className="py-2 text-right">Value</th>
              <th className="py-2 text-right">Remaining</th>
              <th className="py-2 text-right">Net equity</th>
              <th className="py-2 text-right">Net (USD)</th>
            </tr>
          </thead>
          <tbody>
            {data.properties.map((p) => {
              const hasLedger = !!(p.payments && p.payments.length > 0);
              const isOpen = expanded === p.name;
              return (
                <Fragment key={`${p.name}-${p.currency}`}>
                  <tr
                    className={`border-b border-border/40 ${hasLedger ? "cursor-pointer hover:bg-muted/30" : ""}`}
                    onClick={hasLedger ? () => setExpanded(isOpen ? null : p.name) : undefined}
                  >
                    <td className="py-1.5">
                      {hasLedger && <span className="mr-1 text-muted-foreground">{isOpen ? "▾" : "▸"}</span>}
                      {p.name}
                      {p.warnings.length > 0 && (
                        <span
                          className="ml-1.5 text-amber-400 text-xs"
                          title={p.warnings.join("; ")}
                        >
                          ⚠
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 text-right text-muted-foreground">
                      {fmtLocal(p.home_local, p.currency)}
                    </td>
                    <td className="py-1.5 text-right text-muted-foreground">
                      {fmtLocal(p.loan_local, p.currency)}
                    </td>
                    <td className="py-1.5 text-right">
                      {fmtLocal(p.net_local, p.currency)}
                    </td>
                    <td className="py-1.5 text-right">{fmtUsdK(p.net_usd_k)}</td>
                  </tr>
                  {hasLedger && isOpen && (
                    <tr className="border-b border-border/40 bg-muted/20">
                      <td colSpan={5} className="py-2 px-3 text-xs">
                        <div className="mb-1 text-muted-foreground">
                          Paid {fmtLocal(p.paid_to_date_local ?? null, p.currency)} of{" "}
                          {fmtLocal(p.home_local, p.currency)}
                          {p.vat_paid_local
                            ? ` · VAT ${fmtLocal(p.vat_paid_local, p.currency)} (sunk, not equity)`
                            : ""}
                        </div>
                        <table className="w-full">
                          <tbody>
                            {p.payments!.map((pay, i) => (
                              <tr key={i} className="text-muted-foreground">
                                <td className="py-0.5">{pay.payment_date ?? "—"}</td>
                                <td className="py-0.5">{pay.invoice_no ?? pay.kind}</td>
                                <td className="py-0.5">{pay.description}</td>
                                <td className="py-0.5 text-right text-foreground">
                                  {fmtLocal(pay.amount_net_local, p.currency)}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
