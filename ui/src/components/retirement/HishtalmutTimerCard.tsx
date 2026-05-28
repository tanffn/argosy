"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api } from "@/lib/api";
import type {
  HishtalmutEligibilityResponse,
  HishtalmutWithdrawalTaxResponse,
} from "@/lib/retirement-types";

interface Props {
  userId: string;
  firstDepositDate: string;  // "YYYY-MM-DD"
  currentAge: number;
}

export function HishtalmutTimerCard({ userId, firstDepositDate, currentAge }: Props) {
  const [data, setData] = useState<HishtalmutEligibilityResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Withdrawal-tax explorer (sibling endpoint /hishtalmut/withdrawal-tax)
  // — user types a hypothetical gross amount, we surface the tax owed
  // alongside the eligibility status above. Default 100K NIS as a sensible
  // anchor; debounced to 350ms to keep the API call rate low while typing.
  const [grossNis, setGrossNis] = useState<number>(100_000);
  const [tax, setTax] = useState<HishtalmutWithdrawalTaxResponse | null>(null);
  const [taxErr, setTaxErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (api.retirement.hishtalmutEligibility(userId, firstDepositDate, currentAge) as Promise<HishtalmutEligibilityResponse>)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [userId, firstDepositDate, currentAge]);

  useEffect(() => {
    if (grossNis <= 0) return;
    let cancelled = false;
    const handle = window.setTimeout(() => {
      api.retirement
        .hishtalmutWithdrawalTax(userId, firstDepositDate, currentAge, grossNis)
        .then((r) => { if (!cancelled) { setTax(r); setTaxErr(null); } })
        .catch((e) => { if (!cancelled) setTaxErr(e instanceof Error ? e.message : String(e)); });
    }, 350);
    return () => { cancelled = true; window.clearTimeout(handle); };
  }, [userId, firstDepositDate, currentAge, grossNis]);

  // Drive the displayed tax from a derived value so the effect above
  // never needs to setState synchronously when grossNis drops to 0.
  // (When grossNis is 0 or negative the input is "empty" semantically;
  // the prior fetched result would be misleading.)
  const displayedTax = grossNis > 0 ? tax : null;
  const displayedTaxErr = grossNis > 0 ? taxErr : null;

  if (err) return <Card><CardHeader><CardTitle className="text-base">Hishtalmut eligibility</CardTitle><CardDescription className="text-rose-400">{err}</CardDescription></CardHeader></Card>;
  if (!data) return <Card><CardHeader><CardTitle className="text-base">Hishtalmut eligibility</CardTitle><CardDescription>Loading…</CardDescription></CardHeader></Card>;

  const taxfree = (data.taxfree_now.value as number) === 1;
  const months = data.months_until_taxfree.value as number;
  const years = Math.floor(months / 12);
  const remMonths = months % 12;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          Hishtalmut eligibility
          {taxfree ? (
            <span className="text-xs font-mono text-emerald-400">●TAX-FREE NOW</span>
          ) : (
            <span className="text-xs font-mono text-amber-400">●WAITING</span>
          )}
        </CardTitle>
        <CardDescription>
          §3(e) tax-free if 6yr from first deposit OR age 67. Both paths
          checked independently.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="text-3xl font-mono font-semibold">
          {taxfree ? "₪0 tax due" : `${years}y ${remMonths}mo to go`}
        </div>
        <div className="mt-2 grid grid-cols-2 gap-3 text-sm">
          <div>
            <span className="text-muted-foreground">6yr path:</span>{" "}
            <ValueWithTooltip data={data.six_yr_eligible}>
              {(data.six_yr_eligible.value as number) === 1 ? "✓ eligible" : "× waiting"}
            </ValueWithTooltip>
          </div>
          <div>
            <span className="text-muted-foreground">Age-67 path:</span>{" "}
            <ValueWithTooltip data={data.age_67_eligible}>
              {(data.age_67_eligible.value as number) === 1 ? "✓ eligible" : "× waiting"}
            </ValueWithTooltip>
          </div>
          <div>
            <span className="text-muted-foreground">First deposit:</span>{" "}
            <ValueWithTooltip data={data.first_deposit_date} />
          </div>
          <div>
            <span className="text-muted-foreground">If withdrawn now:</span>{" "}
            {taxfree ? "₪0 tax" : `× ${((data.early_withdrawal_marginal_rate.value as number) * 100).toFixed(0)}% marginal`}
          </div>
        </div>

        <div className="mt-5 rounded-md border border-border/40 bg-secondary/30 px-3 py-3">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-2">
            What if I withdraw...
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono text-sm text-muted-foreground">₪</span>
            <input
              type="number"
              min={0}
              step={10_000}
              value={Number.isFinite(grossNis) ? grossNis : 0}
              onChange={(e) => {
                const v = Number(e.target.value);
                setGrossNis(Number.isFinite(v) && v >= 0 ? v : 0);
              }}
              className="w-36 rounded border border-border bg-background/60 px-2 py-1 font-mono text-sm tabular-nums focus:outline-none focus:ring-1 focus:ring-info/50"
              aria-label="Hypothetical gross withdrawal amount in NIS"
            />
            <span className="font-mono text-sm text-muted-foreground">gross</span>
            <span className="font-mono text-sm text-muted-foreground">→ tax owed:</span>
            {displayedTaxErr ? (
              <span className="font-mono text-sm text-rose-400">err</span>
            ) : displayedTax ? (
              <ValueWithTooltip data={displayedTax.tax}>
                <span
                  className={`font-mono text-sm font-semibold ${
                    displayedTax.taxfree_now ? "text-emerald-400" : "text-amber-400"
                  }`}
                >
                  {formatTaxNis(displayedTax.tax.value)}
                </span>
              </ValueWithTooltip>
            ) : (
              <span className="font-mono text-sm text-muted-foreground">&hellip;</span>
            )}
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">
            &#8378;0 when an eligibility path above is satisfied (you keep
            the full gross). Otherwise gross &times; your marginal rate,
            capped at the deposit basis already taxed. Hover the result
            for source + rationale.
          </p>
        </div>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Israeli Income Tax Ordinance §3(e) ships two distinct
              tax-free paths — only ONE must be satisfied:
            </p>
            <ul className="list-disc pl-5">
              <li>6 years from first deposit (employee-deposited)</li>
              <li>Age 67 lump path (any holding period)</li>
            </ul>
            <p>
              Early withdrawal that fails BOTH paths is taxed at the
              user&apos;s marginal income tax rate (~47% top bracket).
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}

function formatTaxNis(value: number | string | null): string {
  if (value === null || value === undefined) return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (n === 0) return "₪0";
  return `₪${Math.round(n).toLocaleString()}`;
}
