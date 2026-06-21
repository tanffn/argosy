"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api, type GateStatus, type GateVerdict, type SafetyGatesResponse } from "@/lib/api";

const STATUS_STYLE: Record<GateStatus, { dot: string; text: string; bar: string; label: string }> = {
  PASS: { dot: "bg-emerald-500", text: "text-emerald-400", bar: "from-emerald-500/20", label: "PASS" },
  WARN: { dot: "bg-amber-500", text: "text-amber-400", bar: "from-amber-500/20", label: "WARN" },
  FAIL: { dot: "bg-rose-500", text: "text-rose-400", bar: "from-rose-500/20", label: "FAIL" },
};

const GATE_TITLES: Record<GateVerdict["gate_id"], string> = {
  nra_estate: "US NRA estate exposure",
  emergency_liquidity: "Emergency liquidity",
  conflict_scenario: "Conflict scenario stress",
};

interface Props {
  userId: string;
}

/**
 * SafetyGatesPanel — Wave 2 visualization.
 *
 * Shows each gate as a tile with status dot, headline number,
 * threshold reference, and a "what to do" callout. Three gates in
 * Waves 2 + 3.6.
 */
export function SafetyGatesPanel({ userId }: Props) {
  const [data, setData] = useState<SafetyGatesResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .safetyGates(userId)
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
          <CardTitle className="text-base">Safety gates</CardTitle>
          <CardDescription className="text-rose-400">Failed: {err}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Safety gates</CardTitle>
          <CardDescription className="flex items-center gap-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Loading…
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const gates = data.gates;
  const passCount = gates.filter((g) => g.status === "PASS").length;
  const warnCount = gates.filter((g) => g.status === "WARN").length;
  const failCount = gates.filter((g) => g.status === "FAIL").length;
  const overall: GateStatus =
    failCount > 0 ? "FAIL" : warnCount > 0 ? "WARN" : "PASS";
  const overallStyle = STATUS_STYLE[overall];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <span className={`inline-block h-2.5 w-2.5 rounded-full ${overallStyle.dot}`} aria-hidden />
          Safety gates
          <span className={`text-xs font-mono ${overallStyle.text}`}>
            {passCount}/{gates.length} passing
          </span>
        </CardTitle>
        <CardDescription>
          Hard-blocks for plan approval. If any gate FAILs, the plan can&apos;t
          be approved until the underlying issue is addressed.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {gates.map((g) => {
            const style = STATUS_STYLE[g.status];
            return (
              <div
                key={g.gate_id}
                className={`rounded-md border border-border/40 bg-gradient-to-b ${style.bar} to-transparent p-3`}
              >
                <div className="flex items-center gap-2">
                  <span className={`inline-block h-2 w-2 rounded-full ${style.dot}`} aria-hidden />
                  <span className="text-[10px] font-mono font-semibold uppercase tracking-wider opacity-80">
                    {style.label}
                  </span>
                  <span className="text-sm font-medium">
                    {GATE_TITLES[g.gate_id]}
                  </span>
                </div>
                <div className="mt-2 text-xl font-mono">
                  <ValueWithTooltip data={g.value} />
                </div>
                <div className="text-[10px] text-muted-foreground">
                  threshold:{" "}
                  <ValueWithTooltip data={g.threshold} />
                </div>
                <div className="mt-2 text-xs text-muted-foreground">
                  {String(g.suggested_action.value ?? "")}
                </div>
              </div>
            );
          })}
        </div>

        <DrilldownSection title="Methodology" defaultOpen={false}>
          <MethodologyPanel>
            <p>
              Each gate has a hard threshold + warn threshold derived from a
              cited source. Verdicts:
            </p>
            <ul className="list-disc pl-5">
              <li>
                <b>NRA estate gate</b>: US-situs assets (NVDA + US-domiciled
                ETFs at Schwab; UCITS + cash excluded) vs. the IRS $60K
                exemption for non-US-persons. FAIL above $200K — plan
                approval requires a UCITS migration plan.
              </li>
              <li>
                <b>Emergency liquidity gate</b>: cash buffer in months of
                essential expenses (essential = burn × 60%). Default
                target: 12 months. FAIL below 6.
              </li>
              <li>
                <b>Conflict scenario gate</b> (Wave 3.6): P(ruin at 85)
                under stressed parameters (σ=0.40, inflation=6%, NIS
                devaluation 30%, market closure 6mo). Ships after Wave 3
                builds the underlying P(ruin) infrastructure.
              </li>
            </ul>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
