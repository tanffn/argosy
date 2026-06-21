"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api, type SigmaCalibrationResponse } from "@/lib/api";

interface Props {
  userId: string;
}

const CLASS_LABEL: Record<string, string> = {
  concentrated_equity: "Concentrated equity",
  us_equity: "US equity (diversified)",
  intl_equity: "International equity",
  emerging_equity: "Emerging markets",
  bonds: "Bonds",
  cash: "Cash / HYSA",
  real_estate: "Real estate",
  other: "Other / unclassified",
};

/**
 * SigmaCalibrationCard — auto-calibrated portfolio volatility from holdings.
 *
 * Replaces the prior hardcoded σ=0.18 with a portfolio-weighted average.
 * For an NVDA-heavy portfolio this lifts σ from 0.18 → ~0.30-0.40,
 * which materially widens the Monte Carlo tail risk and forces the
 * "better safe than sorry" verdict instead of an optimistic one.
 */
export function SigmaCalibrationCard({ userId }: Props) {
  const [data, setData] = useState<SigmaCalibrationResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .sigmaCalibration(userId)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  if (err) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Portfolio volatility (σ)</CardTitle>
          <CardDescription className="text-rose-400">Failed: {err}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Portfolio volatility (σ)</CardTitle>
          <CardDescription className="flex items-center gap-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Loading…
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const sigmaPct = typeof data.sigma_annual.value === "number"
    ? (data.sigma_annual.value * 100).toFixed(1)
    : "—";
  const isConcentrated = (typeof data.sigma_annual.value === "number")
    && data.sigma_annual.value > 0.25;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          Portfolio volatility (σ)
          {isConcentrated ? (
            <span className="text-xs font-mono text-rose-400">●CONCENTRATED</span>
          ) : (
            <span className="text-xs font-mono text-emerald-400">●DIVERSIFIED</span>
          )}
        </CardTitle>
        <CardDescription>
          Auto-calibrated from your actual holdings. Drives the Monte Carlo
          tail risk in the projection above.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="text-3xl font-mono font-semibold">
          <ValueWithTooltip data={data.sigma_annual} display={`${sigmaPct}%`} />
        </div>
        <p className="text-xs text-muted-foreground mt-1">
          {isConcentrated
            ? "Concentrated portfolio — diversification would narrow this band materially."
            : "Diversified portfolio — σ within historical S&P 500 range."}
        </p>

        <DrilldownSection title="Per-class breakdown" defaultOpen={false}>
          <ul className="space-y-1 text-sm">
            {data.breakdown.map((b) => (
              <li key={b.asset_class} className="grid grid-cols-[1fr_auto_auto_auto] gap-2">
                <span className="text-muted-foreground">
                  {CLASS_LABEL[b.asset_class] ?? b.asset_class}
                </span>
                <span className="font-mono text-xs">{b.weight_pct.toFixed(1)}%</span>
                <span className="font-mono text-xs text-muted-foreground">
                  σ={b.sigma.toFixed(2)}
                </span>
                <span className="font-mono text-xs text-muted-foreground">
                  +{(b.contribution * 100).toFixed(2)}pp
                </span>
              </li>
            ))}
          </ul>
        </DrilldownSection>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Portfolio σ is computed as the weighted average of asset-class
              volatilities: σ_portfolio = Σ (weight_i × σ_i).
            </p>
            <p>
              Asset-class σ defaults (historical post-1970):
            </p>
            <ul className="list-disc pl-5">
              <li>Concentrated equity (NVDA, TSLA, ...): σ ≈ 0.45</li>
              <li>Diversified US equity (S&P 500): σ ≈ 0.18</li>
              <li>International developed: σ ≈ 0.20</li>
              <li>Emerging markets: σ ≈ 0.25</li>
              <li>Bonds (investment-grade): σ ≈ 0.06</li>
              <li>Cash / HYSA: σ ≈ 0.02</li>
            </ul>
            <p>
              Weighted average is a conservative approximation; it slightly
              overstates σ vs. the true variance-covariance computation
              (which credits diversification benefit). The &quot;better safe
              than sorry&quot; bias is intentional.
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
