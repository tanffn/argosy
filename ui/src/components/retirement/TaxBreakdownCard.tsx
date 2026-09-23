"use client";

import { useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { apiUrl } from "@/lib/api-base";
import type { TaxBreakdownResponse } from "@/lib/retirement-types";

interface Props {
  userId: string;
}

type Source = "capital_gain" | "dividend_us_source" | "dividend_israeli_source" | "pension_annuity" | "salary" | "rsu_vest";

export function TaxBreakdownCard({ userId }: Props) {
  const [source, setSource] = useState<Source>("capital_gain");
  const [gross, setGross] = useState(100_000);
  const [pensionExemption, setPensionExemption] = useState(0);
  const [data, setData] = useState<TaxBreakdownResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function compute() {
    setLoading(true);
    setErr(null);
    try {
      const res = await fetch(apiUrl(`/api/retirement/tax/compute?user_id=${encodeURIComponent(userId)}`), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          source,
          gross_amount_nis: gross,
          us_gross_amount_for_treaty: source === "dividend_us_source" ? gross : 0,
          is_post_67: source === "pension_annuity",
          pension_period_months: 1,
          pension_exemption_monthly_nis: source === "pension_annuity" ? pensionExemption : 0,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Tax calculator</CardTitle>
        <CardDescription>
          Tax estimates with stated assumptions, not a personal tax assessment.
          Pension exemption is limited by the qualifying ceiling and personal rights;
          it is not a percentage of the whole pension.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex flex-wrap items-end gap-3 text-sm mb-3">
          <label className="flex flex-col">
            <span className="text-xs text-muted-foreground">Source</span>
            <select value={source} onChange={(e) => setSource(e.target.value as Source)} className="mt-1 rounded border border-border/60 bg-background px-2 py-1">
              <option value="capital_gain">Capital gain (Israeli equity)</option>
              <option value="dividend_us_source">Ordinary US dividend (eligible treaty / FTC)</option>
              <option value="dividend_israeli_source">Israeli dividend</option>
              <option value="pension_annuity">Pension annuity (post-67)</option>
              <option value="salary">Salary</option>
              <option value="rsu_vest">RSU vest</option>
            </select>
          </label>
          <label className="flex flex-col">
            <span className="text-xs text-muted-foreground">{source === "pension_annuity" ? "Monthly gross pension (NIS)" : "Gross amount (NIS)"}</span>
            <input type="number" value={gross} onChange={(e) => setGross(Number(e.target.value))} className="mt-1 rounded border border-border/60 bg-background px-2 py-1 w-32" />
          </label>
          {source === "pension_annuity" && (
            <label className="flex flex-col">
              <span className="text-xs text-muted-foreground">Personal monthly exemption (NIS; default 0, 2026 maximum 5,422)</span>
              <input type="number" min={0} max={5422} value={pensionExemption} onChange={(e) => setPensionExemption(Number(e.target.value))} className="mt-1 rounded border border-border/60 bg-background px-2 py-1 w-32" />
            </label>
          )}
          <button onClick={compute} disabled={loading} className="rounded bg-foreground/10 px-3 py-1.5 text-sm hover:bg-foreground/20">
            {loading ? "Computing…" : "Compute"}
          </button>
        </div>

        {err && <p className="text-sm text-rose-400">{err}</p>}

        {data && (
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mt-3">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Gross</div>
              <div className="text-lg font-mono"><ValueWithTooltip data={data.gross} /></div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Israeli tax</div>
              <div className="text-lg font-mono text-rose-400"><ValueWithTooltip data={data.israeli_tax} /></div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">US withholding (cash tax)</div>
              <div className="text-lg font-mono text-rose-400"><ValueWithTooltip data={data.us_withholding} /></div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">US credit against Israeli base tax (not extra cash)</div>
              <div className="text-lg font-mono text-sky-400"><ValueWithTooltip data={data.us_treaty_credit} /></div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Surtax</div>
              <div className="text-lg font-mono text-rose-400"><ValueWithTooltip data={data.surtax} /></div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Bituach Leumi</div>
              <div className="text-lg font-mono text-rose-400"><ValueWithTooltip data={data.bituach_leumi_tax} /></div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Net</div>
              <div className="text-lg font-mono text-emerald-400"><ValueWithTooltip data={data.net} /></div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Effective rate</div>
              <div className="text-lg font-mono"><ValueWithTooltip data={data.effective_rate} /></div>
            </div>
          </div>
        )}

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Source-specific rules per Israeli law + US-Israel treaty:
            </p>
            <ul className="list-disc pl-5 text-xs">
              <li><b>Capital gain</b>: flat 25% Israeli CGT on equity</li>
              <li><b>Ordinary US dividend</b>: assumes an eligible Israeli-resident individual with valid W-8BEN, 25% US withholding and a fully usable credit against the 25% Israeli base tax. Net cash subtracts US withholding plus remaining Israeli tax and applicable surtax. This is not actual broker withholding or a model for exempt fund distributions, REIT exceptions or taxpayer-specific credit treatment.</li>
              <li><b>Pension</b>: 2026 maximum is 57.5% of the qualifying ceiling of ₪9,430/month (published exemption ₪5,422), not 57.5% of your pension. Supply your usable monthly entitlement after rights fixation, grants and capital withdrawals. Default zero does not mean you have no legal rights; it avoids assuming unverified ones. Taxable pension uses the selected marginal-rate estimate, not the full progressive tax/credit calculation. Future statutory fraction is 62.5% in 2027 and 67% from 2028; future nominal ceilings require fresh data.</li>
              <li><b>Salary / RSU</b>: marginal 47% + BL 7% capped at ₪50K insurable ceiling</li>
            </ul>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
