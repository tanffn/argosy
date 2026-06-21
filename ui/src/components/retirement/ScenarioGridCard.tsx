"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { api } from "@/lib/api";
import type { ScenarioGridResponse } from "@/lib/retirement-types";

interface Props {
  userId: string;
  retirementAge?: number;
}

const pct = (v: number) => `${(v * 100).toFixed(0)}%`;
const nis = (v: number) => `₪${Math.round(v).toLocaleString()}`;

/** Colour the P(solvent) cell so the reader sees risk at a glance. */
function solventClass(v: number): string {
  if (v >= 0.9) return "text-emerald-400";
  if (v >= 0.75) return "text-amber-400";
  return "text-rose-400";
}

const ROW_TONE: Record<string, string> = {
  base: "bg-sky-500/10",
  bull: "",
  bear: "",
};

/**
 * ScenarioGridCard — the decision-surface readiness table (codex MC review
 * 2026-06-04). Base / bull / bear are genuine SCENARIOS (a real return
 * assumption + an explicit sequence-risk shock for bear), not ±1σ value bands.
 * Every number is computed at the permanent-equivalent spend basis with
 * Bituach Leumi income credited, so it reconciles with the FI target. The
 * regime-switch fat-tail figure is kept as a clearly-labelled secondary
 * readout.
 */
export function ScenarioGridCard({ userId, retirementAge = 49 }: Props) {
  const [data, setData] = useState<ScenarioGridResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .projectionScenarios(userId, { retirementAge, seed: 42 })
      .then((d) => {
        if (!cancelled) {
          setErr(null);
          setData(d);
        }
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [userId, retirementAge]);

  if (err) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Retirement readiness — scenario stress</CardTitle>
          <CardDescription className="text-rose-400">Failed: {err}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Retirement readiness — scenario stress</CardTitle>
          <CardDescription className="flex items-center gap-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Running scenario Monte Carlo…
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  // bear (3%) → base (4.5%) → bull (5.5%)
  const scenarios = [...data.scenarios].sort((a, b) => a.mu_real_pct - b.mu_real_pct);
  const grid = [...data.mu_grid].sort((a, b) => a.mu_real_pct - b.mu_real_pct);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Retirement readiness — scenario stress</CardTitle>
        <CardDescription>
          P(portfolio solvent) by scenario, retiring at age {data.retirement_age}, horizon to 95.
        </CardDescription>
        {/* Provenance chips — every basis input traces to a source. */}
        <div className="mt-2 flex flex-wrap gap-1.5">
          <Chip
            label={`Spend basis ${nis(data.spend_basis_annual_nis)}/yr`}
            title={`permanent-equivalent · ${data.spend_basis_source}`}
          />
          <Chip
            label={
              data.bl_monthly_nis > 0
                ? `BL ${nis(data.bl_monthly_nis)}/mo credited @67`
                : "BL not credited"
            }
            title={data.bl_source}
          />
          <Chip
            label={`annuity tax ${pct(data.annuity_tax_rate)}`}
            title={`net-of-tax pension annuity · ${data.annuity_tax_source}`}
          />
          <Chip label={`σ ${pct(data.sigma_annual)}`} title="portfolio volatility (annualized)" />
          <Chip label={`inflation ${pct(data.inflation_annual)}`} title="CPI assumption" />
        </div>
      </CardHeader>
      <CardContent>
        {/* Scenario table */}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-muted-foreground">
                <th className="text-left font-medium py-1.5">Scenario</th>
                <th className="text-right font-medium">μ real</th>
                <th className="text-right font-medium">shock</th>
                <th className="text-right font-medium">@75</th>
                <th className="text-right font-medium">@85</th>
                <th className="text-right font-medium">@95</th>
              </tr>
            </thead>
            <tbody className="font-mono tabular-nums">
              {scenarios.map((s) => (
                <tr key={s.name} className={`${ROW_TONE[s.name] ?? ""} border-t border-border/40`}>
                  <td className="py-1.5 font-sans text-foreground">{s.label}</td>
                  <td className="text-right">{pct(s.mu_real_pct)}</td>
                  <td className="text-right text-muted-foreground">
                    {s.initial_shock_pct > 0 ? `−${pct(s.initial_shock_pct)}` : "—"}
                  </td>
                  <td className={`text-right ${solventClass(s.p_solvent_75)}`}>{pct(s.p_solvent_75)}</td>
                  <td className={`text-right ${solventClass(s.p_solvent_85)}`}>{pct(s.p_solvent_85)}</td>
                  <td className={`text-right font-semibold ${solventClass(s.p_solvent_95)}`}>
                    {pct(s.p_solvent_95)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* μ-grid sensitivity */}
        <div className="mt-4">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
            Return-assumption sensitivity — P(solvent at 95), no shock
          </div>
          <div className="grid grid-cols-4 gap-2 font-mono tabular-nums text-sm">
            {grid.map((p) => (
              <div key={p.mu_real_pct} className="rounded border border-border/40 px-2 py-1.5 text-center">
                <div className="text-[11px] text-muted-foreground">{pct(p.mu_real_pct)} real</div>
                <div className={`text-base font-semibold ${solventClass(p.p_solvent_95)}`}>
                  {pct(p.p_solvent_95)}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Secondary sensitivities */}
        <div className="mt-4 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
          <span>
            Current-burn sensitivity ({nis(data.spend_t12_annual_nis)}/yr):{" "}
            <span className={`font-mono font-semibold ${solventClass(data.t12_sensitivity_p_solvent_95)}`}>
              {pct(data.t12_sensitivity_p_solvent_95)}
            </span>{" "}
            @95
          </span>
          <span>
            Fat-tail stress (3-regime Markov):{" "}
            <span className={`font-mono font-semibold ${solventClass(data.fat_tail_p_solvent_95)}`}>
              {pct(data.fat_tail_p_solvent_95)}
            </span>{" "}
            @95 · clustered-crash downside, secondary
          </span>
        </div>

        <DrilldownSection title="Methodology" defaultOpen={false}>
          <MethodologyPanel>
            <p>
              These are <b>scenarios</b>, not ±1σ value bands. Each runs a{" "}
              {data.n_paths}-path Monte Carlo at the permanent-equivalent spend
              basis ({nis(data.spend_basis_annual_nis)}/yr — the number the FI
              target was sized on, not the lower current burn) with Bituach
              Leumi income netted against spend from age 67.
            </p>
            <ul className="list-disc pl-5">
              <li><b>Base</b> — 4.5% real return, no shock. The decision-central case.</li>
              <li><b>Bull</b> — 5.5% real, no shock.</li>
              <li>
                <b>Bear</b> — an immediate −{pct(scenarios.find((s) => s.name === "bear")?.initial_shock_pct ?? 0.25)}{" "}
                hit to the liquid portfolio at retirement, then a low-return
                decade (sequence risk is the load-bearing retirement risk), then
                recovery.
              </li>
            </ul>
            <p>
              The lognormal engine carries the scenario grid (it is responsive
              to the return assumption). The 3-regime Markov engine carries the
              fat-tail readout — at the <i>same</i> basis + BL, so the two
              numbers are comparable. Withdrawals are grossed up for tax on the
              taxable-gain fraction of each sale (≈15% effective), not the full
              withdrawal.
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}

function Chip({ label, title }: { label: string; title: string }) {
  return (
    <span
      title={title}
      className="inline-flex items-center rounded-full border border-border/60 bg-muted/40 px-2 py-0.5 text-[11px] font-mono text-muted-foreground"
    >
      {label}
    </span>
  );
}
