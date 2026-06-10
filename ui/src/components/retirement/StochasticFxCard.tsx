"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api, type FxBandResponse } from "@/lib/api";

interface Props {
  initialFx?: number;
  horizonMonths?: number;
}

/**
 * StochasticFxCard — USD/NIS forecast band at the retirement horizon.
 *
 * Replaces the prior single-snapshot FX assumption with a lognormal
 * random walk. Shows P10/P25/P50/P75/P90 at the chosen horizon (default 30y).
 */
export function StochasticFxCard({
  initialFx = 3.4,
  horizonMonths = 360,
}: Props) {
  const [data, setData] = useState<FxBandResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .stochasticFx(initialFx, horizonMonths)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [initialFx, horizonMonths]);

  if (err) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">USD/NIS forecast band</CardTitle>
          <CardDescription className="text-rose-400">Failed: {err}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">USD/NIS forecast band</CardTitle>
          <CardDescription>Running stochastic FX…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const yearsOut = (horizonMonths / 12).toFixed(0);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          USD/NIS forecast — {yearsOut}y horizon
        </CardTitle>
        <CardDescription>
          Lognormal random walk from today&apos;s ₪{initialFx.toFixed(2)}/$ with
          σ_fx=0.08. NIS-denominated liabilities + USD-denominated assets
          mean FX drift materially affects retire-ready age.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-5 gap-2 text-center">
          {(["p10", "p25", "p50", "p75", "p90"] as const).map((key) => (
            <div key={key}>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                {key.toUpperCase()}
              </div>
              <div className="mt-1 text-lg font-mono font-semibold">
                <ValueWithTooltip data={data.bands[key]} />
              </div>
            </div>
          ))}
        </div>
        <div className="mt-3 text-xs text-muted-foreground text-center">
          P10 ↔ P90 spread: ₪
          {(((data.bands.p90.value as number) - (data.bands.p10.value as number)) || 0).toFixed(2)}
          /$ at the {yearsOut}y horizon.
        </div>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Lognormal random walk on USD/NIS:
            </p>
            <p>
              <code>log(fx_t+1 / fx_t) ~ N(μ_fx/12 - σ_fx²/24, σ_fx/√12)</code>
            </p>
            <ul className="list-disc pl-5">
              <li>μ_fx = 0 (no long-term drift assumed; USD/NIS has been roughly mean-reverting around 3.3-3.7 post-2000)</li>
              <li>σ_fx = 0.08 annualized (post-2000 realized vol)</li>
              <li>n_paths = 1000</li>
            </ul>
            <p>
              A 30% NIS strengthening (toward P10) turns &quot;retire-ready at
              49&quot; into &quot;retire-ready at 56&quot; for a USD-asset / NIS-liability
              household — this is the #1 silent risk in the prior projection
              that used a fixed snapshot FX.
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
