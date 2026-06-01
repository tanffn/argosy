"use client";

/**
 * Wave 8 Piece C — Assumption-defaults card.
 *
 * Renders the six pre-populated cashflow-projection assumptions
 * (mu / sigma / tax / inflation / retirement age / lifestyle drift)
 * with their per-field `▸ why?` rationale tooltips. Sourced from
 * /api/plan/current/cashflow-default-assumptions, which auto-detects
 * sigma from the sigma calibrator + reads goals_yaml for the
 * user-stated fields + falls back to hardcoded defaults with a clear
 * rationale on every field.
 */

import { useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type {
  AssumptionFieldDTO,
  DefaultAssumptionsResponseDTO,
} from "@/lib/api";

interface AssumptionsCardProps {
  defaults: DefaultAssumptionsResponseDTO | null;
}

interface FieldSpec {
  key: keyof DefaultAssumptionsResponseDTO;
  label: string;
  format: (v: number) => string;
}

const FIELDS: FieldSpec[] = [
  {
    key: "mu_nominal_annual",
    label: "Expected return (μ)",
    format: (v) => `${(v * 100).toFixed(2)}% / yr`,
  },
  {
    key: "sigma_annual",
    label: "Volatility (σ)",
    format: (v) => `${(v * 100).toFixed(1)}% / yr`,
  },
  {
    key: "tax_rate",
    label: "Tax rate",
    format: (v) => `${(v * 100).toFixed(1)}%`,
  },
  {
    key: "inflation_annual",
    label: "Inflation",
    format: (v) => `${(v * 100).toFixed(2)}% / yr`,
  },
  {
    key: "retirement_age",
    label: "Retirement age",
    format: (v) => `age ${v.toFixed(0)}`,
  },
  {
    key: "lifestyle_drift_annual",
    label: "Lifestyle drift",
    format: (v) => `${(v * 100).toFixed(2)}% / yr above CPI`,
  },
];

function sourceLabel(s: AssumptionFieldDTO["source"]): string {
  switch (s) {
    case "sigma_calibrator":
      return "calibrated";
    case "goals_yaml":
      return "your goals";
    case "default":
      return "default";
  }
}

function sourceBadgeVariant(
  s: AssumptionFieldDTO["source"],
): "default" | "secondary" | "outline" {
  if (s === "default") return "outline";
  return "secondary";
}

export function AssumptionsCard({ defaults }: AssumptionsCardProps) {
  const [openIdx, setOpenIdx] = useState<number | null>(null);
  if (defaults == null) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Cashflow assumptions</CardTitle>
          <CardDescription>
            Defaults unavailable — calibration endpoint did not respond.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Cashflow assumptions</CardTitle>
        <CardDescription>
          Pre-populated defaults the projection uses. Click any field to
          see WHY it has that value (calibrated from your portfolio,
          read from goals_yaml, or a hardcoded fallback with rationale).
          Override the values in the projection chart below.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="flex flex-col gap-1.5">
          {FIELDS.map((field, i) => {
            const f = defaults[field.key];
            const isOpen = openIdx === i;
            return (
              <li
                key={field.key}
                className="border border-border/60 rounded-md p-2"
              >
                <button
                  type="button"
                  onClick={() => setOpenIdx(isOpen ? null : i)}
                  className="w-full text-left flex items-center justify-between gap-2"
                >
                  <span className="text-sm font-medium">{field.label}</span>
                  <span className="flex items-center gap-2">
                    <span className="font-mono text-sm">
                      {field.format(f.value)}
                    </span>
                    <Badge
                      variant={sourceBadgeVariant(f.source)}
                      className="text-[10px]"
                    >
                      {sourceLabel(f.source)}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      {isOpen ? "▼" : "▸"} why?
                    </span>
                  </span>
                </button>
                {isOpen ? (
                  <p className="text-xs text-muted-foreground mt-2 border-t border-border/40 pt-2">
                    {f.rationale_md}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}
