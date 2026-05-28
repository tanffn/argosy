"use client";

import { useEffect, useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { HeroCard } from "@/components/retirement/HeroCard";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { SourcesPanel } from "@/components/retirement/SourcesPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api, type ValueWithRationale } from "@/lib/api";

const USER_ID = "ariel";

/**
 * Retirement companion page — scaffold for the 7-wave overhaul.
 *
 * Wave 0 (current): hero placeholder + drilldown skeleton + sources panel.
 * Later waves populate the real verdict + chart + safety gates + glide path
 * etc.
 *
 * Plan: docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md
 */
export default function RetirementPage() {
  const [mekadem, setMekadem] = useState<ValueWithRationale | null>(null);
  const [bl, setBl] = useState<ValueWithRationale | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api.retirement
      .reference("mekadem.clal_pensia", USER_ID)
      .then((d) => {
        if (!cancelled) setMekadem(d);
      })
      .catch(() => {
        /* placeholder — Wave 0 scaffold tolerates missing data */
      });
    void api.retirement
      .reference("bituach_leumi.single_age_67_base_2026", USER_ID)
      .then((d) => {
        if (!cancelled) setBl(d);
      })
      .catch(() => {
        /* placeholder */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="container mx-auto px-4 py-6 max-w-5xl space-y-4">
      <HeroCard
        title="Retirement readiness"
        status="UNCERTAIN"
        verdict="Wave 0 scaffold — full verdict ships in Wave 3 (probability-of-ruin gate)."
        numbers={[
          {
            label: "P(solvent at 95)",
            display: "—",
            secondary: "Wave 3",
          },
          {
            label: "Retire-ready age",
            display: "—",
            secondary: "Wave 3",
          },
          {
            label: "Safety gates",
            display: "—",
            secondary: "Wave 2",
          },
        ]}
      />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Foundational reference values (Wave 0 smoke test)
          </CardTitle>
          <CardDescription>
            These come from the new hybrid-defaults resolver. Hover over the
            values to see the source + rationale.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <div>
            <span className="text-muted-foreground">Clal mekadem:</span>{" "}
            {mekadem ? (
              <ValueWithTooltip data={mekadem} />
            ) : (
              <span className="text-muted-foreground">loading…</span>
            )}
          </div>
          <div>
            <span className="text-muted-foreground">
              Bituach Leumi old-age stipend (single, age 67):
            </span>{" "}
            {bl ? (
              <ValueWithTooltip data={bl} />
            ) : (
              <span className="text-muted-foreground">loading…</span>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Coming up</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          <ul className="list-disc pl-5 space-y-1">
            <li>
              <b>Wave 1:</b> Mekadem variance band + Bituach Leumi stipend module
            </li>
            <li>
              <b>Wave 2:</b> Safety gates (NRA estate · emergency liquidity)
            </li>
            <li>
              <b>Wave 3:</b> Probability-of-ruin gate + sigma auto-calibration +
              regime-switch MC + stochastic FX + withdrawal policy + conflict
              scenario gate
            </li>
            <li>
              <b>Wave 4:</b> Glide path + rebalancing + lifecycle income + phase
              expenses + IDF service + healthcare
            </li>
            <li>
              <b>Wave 5:</b> Account-aware tax engine + decumulation + lump-vs-
              annuity + hishtalmut + kupat-gemel
            </li>
            <li>
              <b>Wave 6:</b> Real estate · mortgage · partner · severance split
            </li>
            <li>
              <b>Wave 7:</b> Insurance gaps + action engine + replan triggers +
              multi-goal + behavioral + route dedup
            </li>
          </ul>
        </CardContent>
      </Card>

      <DrilldownSection title="Methodology" defaultOpen={false}>
        <MethodologyPanel>
          <p>
            The retirement companion follows a "hero + chart + drill-down"
            standard. Top: a verdict card with the one-line answer + 1-3 key
            numbers. Middle: the relevant chart (cashflow / Monte Carlo /
            glide path). Bottom: collapsible drill-down sections like this
            one for the methodology, sensitivity analysis, and sources.
          </p>
          <p>
            Every value on the page passes through the{" "}
            <code>ValueWithRationale</code> shape: the value plus its source
            plus its rationale plus any freshness warnings. Hover over any
            dotted-underline number to see the explanation.
          </p>
        </MethodologyPanel>
      </DrilldownSection>

      <DrilldownSection title="Sources" badge="all">
        <SourcesPanel filterIds={null} />
      </DrilldownSection>
    </div>
  );
}
