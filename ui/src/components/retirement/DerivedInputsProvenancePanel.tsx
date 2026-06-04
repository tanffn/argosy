"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import type { DerivedInputField, DerivedInputsResponse } from "@/lib/api";

interface Props {
  data: DerivedInputsResponse | null;
}

/** Human label for each derived-inputs key. */
const LABELS: Record<string, string> = {
  current_age: "Current age",
  retirement_age: "Retirement age",
  fx_usd_nis: "FX (USD→NIS)",
  mekadem_typical: "Mekadem (annuity divisor)",
  bl_contribution_history_years: "BL insured years",
  monthly_need_nis: "Monthly need (permanent-equiv)",
  monthly_burn_nis: "Monthly burn (T12)",
  monthly_income_nis: "Monthly income",
  hishtalmut_balance_nis: "Keren hishtalmut",
  kupat_gemel_balance_nis: "Kupat gemel",
  pension_balance_nis: "Kupat pensia",
  executive_insurance_nis: "Executive insurance",
  taxable_balance_nis: "Taxable brokerage",
  net_worth_nis: "Net worth",
  mortgage_balance_nis: "Mortgage balance",
  residence_value_nis: "Residence value",
  dependents_count: "Dependents",
  has_kids_under_18: "Kids under 18",
  fi_target_nis: "FI perpetuity target",
  fi_total_capital_nis: "FI total capital",
  liquidity_reserve_nis: "Liquidity reserve",
  fire_bridge_requirement_nis: "FIRE bridge (49→60)",
  required_real_yield_pct: "Required real yield",
  expected_real_return_pct: "Expected real return",
  nvda_cap_pct: "NVDA cap",
  nvda_current_pct: "NVDA current",
};

const CONF_TONE: Record<string, string> = {
  HIGH: "text-emerald-400 border-emerald-500/40",
  MEDIUM: "text-amber-400 border-amber-500/40",
  LOW: "text-rose-400 border-rose-500/40",
};

function fmtValue(f: DerivedInputField<number | boolean>): string {
  if (f.status === "pending" || f.value === null || f.value === undefined) return "needs intake";
  const v = f.value;
  if (typeof v === "boolean") return v ? "yes" : "no";
  switch (f.unit) {
    case "nis":
      return `₪${Math.round(v).toLocaleString()}`;
    case "pct":
      return `${(v * 100).toFixed(2)}%`;
    case "age":
      return `${v.toFixed(1)}`;
    case "fx":
      return v.toFixed(3);
    default:
      return `${v}`;
  }
}

/**
 * DerivedInputsProvenancePanel — the audit surface behind every number on
 * /retirement (output-trust doctrine: every figure must be traceable to a
 * source, never a hardcoded magic number). Lists each derived input with its
 * value, source locator, confidence, and resolved/pending status. A pending
 * field is shown as "needs intake", never faked.
 */
export function DerivedInputsProvenancePanel({ data }: Props) {
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Inputs provenance</CardTitle>
          <CardDescription>Loading derived inputs…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const rows = Object.entries(data).filter(
    ([k, v]) => k !== "decision_run_id" && typeof v === "object" && v !== null,
  ) as [string, DerivedInputField<number | boolean>][];

  const pendingCount = rows.filter(([, f]) => f.status === "pending").length;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Inputs provenance</CardTitle>
        <CardDescription>
          Every figure on this page traces to a source — no hardcoded numbers.
          {data.decision_run_id != null && (
            <span className="ml-1 font-mono text-[11px] text-muted-foreground/80">
              · tracking decision run {data.decision_run_id}
            </span>
          )}
          {pendingCount > 0 && (
            <span className="ml-1 text-amber-400">· {pendingCount} need intake</span>
          )}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <DrilldownSection title={`Audit table (${rows.length} inputs)`} defaultOpen={false}>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  <th className="text-left font-medium py-1">Input</th>
                  <th className="text-right font-medium">Value</th>
                  <th className="text-left font-medium pl-3">Source</th>
                  <th className="text-center font-medium">Conf.</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(([key, f]) => {
                  const pending = f.status === "pending";
                  return (
                    <tr key={key} className="border-t border-border/30 align-top">
                      <td className="py-1 pr-2 text-foreground">{LABELS[key] ?? key}</td>
                      <td className={`text-right font-mono tabular-nums ${pending ? "text-amber-400" : ""}`}>
                        {fmtValue(f)}
                      </td>
                      <td className="pl-3 font-mono text-[10px] text-muted-foreground break-all">
                        {f.source}
                      </td>
                      <td className="text-center">
                        <span
                          className={`inline-block rounded border px-1 text-[9px] font-mono ${
                            CONF_TONE[f.confidence] ?? "text-muted-foreground border-border/40"
                          }`}
                        >
                          {f.confidence}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
