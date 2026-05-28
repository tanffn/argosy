"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api, type RuinProbabilityResponse, type Verdict } from "@/lib/api";

const VERDICT_STYLE: Record<Verdict, { dot: string; text: string; bar: string; label: string }> = {
  ON_TRACK: { dot: "bg-emerald-500", text: "text-emerald-400", bar: "from-emerald-500/15 to-transparent", label: "ON TRACK" },
  WARN: { dot: "bg-amber-500", text: "text-amber-400", bar: "from-amber-500/15 to-transparent", label: "WARN" },
  OFF_TRACK: { dot: "bg-rose-500", text: "text-rose-400", bar: "from-rose-500/15 to-transparent", label: "OFF TRACK" },
  UNCERTAIN: { dot: "bg-slate-400", text: "text-slate-300", bar: "from-slate-500/15 to-transparent", label: "UNCERTAIN" },
};

interface Props {
  userId: string;
  retirementAge?: number;
  targetPSolvent?: number;
  withdrawalPolicyId?: "bengen_4pct" | "guyton_klinger" | "vpw" | "bucket";
}

/**
 * RuinProbabilityHero — replaces the misleading single-month "retire-ready"
 * verdict with a probability-of-ruin gate that uses bootstrap CIs to avoid
 * flipping the gate on MC noise.
 *
 * Visualization: hero card with status dot + verdict label, three
 * P(solvent at age) values, CI on the age-95 estimate, suggested-action
 * callout, methodology drilldown.
 */
export function RuinProbabilityHero({
  userId,
  retirementAge = 49,
  targetPSolvent = 0.90,
  withdrawalPolicyId = "guyton_klinger",
}: Props) {
  const [data, setData] = useState<RuinProbabilityResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setErr(null);
    api.retirement
      .ruinProbability(userId, {
        retirementAge,
        targetPSolvent,
        seed: 42,
        withdrawalPolicyId,
      })
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [userId, retirementAge, targetPSolvent, withdrawalPolicyId]);

  if (err) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Retirement readiness</CardTitle>
          <CardDescription className="text-rose-400">Failed: {err}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Retirement readiness</CardTitle>
          <CardDescription>Running Monte Carlo…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const style = VERDICT_STYLE[data.verdict];
  const pct = (v: number) => `${(v * 100).toFixed(0)}%`;
  const ciLow = typeof data.p_solvent_at_95_ci_low.value === "number"
    ? data.p_solvent_at_95_ci_low.value : 0;
  const ciHigh = typeof data.p_solvent_at_95_ci_high.value === "number"
    ? data.p_solvent_at_95_ci_high.value : 0;
  const target = typeof data.target_p_solvent.value === "number"
    ? data.target_p_solvent.value : 0.9;

  return (
    <Card className={`relative overflow-hidden bg-gradient-to-r ${style.bar}`}>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <span className={`inline-block h-2.5 w-2.5 rounded-full ${style.dot}`} aria-hidden />
          Retirement readiness
          <span className={`text-xs font-mono font-semibold ${style.text}`}>
            {style.label}
          </span>
        </CardTitle>
        <CardDescription>
          P(solvent through 95) under {target * 100}% target, retiring at age{" "}
          {retirementAge}, with sequence-of-returns risk modeled.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              P(solvent at 75)
            </div>
            <div className="mt-1 text-2xl font-mono font-semibold">
              <ValueWithTooltip
                data={data.p_solvent_at_75}
                display={pct(typeof data.p_solvent_at_75.value === "number" ? data.p_solvent_at_75.value : 0)}
              />
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              P(solvent at 85)
            </div>
            <div className="mt-1 text-2xl font-mono font-semibold">
              <ValueWithTooltip
                data={data.p_solvent_at_85}
                display={pct(typeof data.p_solvent_at_85.value === "number" ? data.p_solvent_at_85.value : 0)}
              />
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              P(solvent at 95)
            </div>
            <div className="mt-1 text-2xl font-mono font-semibold">
              <ValueWithTooltip
                data={data.p_solvent_at_95}
                display={pct(typeof data.p_solvent_at_95.value === "number" ? data.p_solvent_at_95.value : 0)}
              />
            </div>
            <div className="text-[10px] text-muted-foreground">
              95% CI: [{pct(ciLow)} – {pct(ciHigh)}]
            </div>
          </div>
        </div>

        <p className="mt-4 text-sm text-muted-foreground">
          {String(data.suggested_action.value ?? "")}
        </p>

        <DrilldownSection title="Methodology" defaultOpen={false}>
          <MethodologyPanel>
            <p>
              The verdict comes from a 2000-path Monte Carlo simulation of
              monthly portfolio returns under sequence-of-returns risk.
              Paths that hit zero stay at zero — once you've exhausted
              liquid assets, the pension lump unlock at 60 does NOT rescue
              you (correct model semantics for "running out of money").
            </p>
            <p>
              Verdict logic uses a bootstrap 95% CI rather than the point
              estimate so noisy MC near the threshold lands in{" "}
              <code>UNCERTAIN</code> rather than flipping the gate:
            </p>
            <ul className="list-disc pl-5">
              <li>
                <b>ON_TRACK</b>: CI lower bound ≥ target ({pct(target)})
              </li>
              <li>
                <b>OFF_TRACK</b>: CI upper bound &lt; target
              </li>
              <li>
                <b>UNCERTAIN</b>: CI straddles target — more paths needed,
                or accept the breadth of uncertainty shown
              </li>
            </ul>
            <p>
              Replaces the prior "retire-ready age" calculation that
              ignored sequence-of-returns risk by checking only a single-
              month income ≥ expenses crossing under deterministic
              assumptions — a user could "retire" right before a 2008-
              style sequence and find themselves OFF_TRACK within 2 years.
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
