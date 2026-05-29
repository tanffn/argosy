"use client";

import { AlertTriangle, AlertCircle, ChevronRight, Info } from "lucide-react";
import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type AnomalyCard } from "@/lib/expenses/api";
import { cn } from "@/lib/utils";

interface AnomalyHighlightsProps {
  anomalies: AnomalyCard[];
}

const ICON_BY_SEVERITY = {
  red: AlertCircle,
  yellow: AlertTriangle,
  info: Info,
} as const;

const COLOR_BY_SEVERITY = {
  red: "text-error",
  yellow: "text-warning",
  info: "text-info",
} as const;

// Sprint #2 commits #10–#11 — kind-specific emoji glyph + color override
// for the three new detector kinds. The legacy 7 kinds keep using the
// severity-based lucide icon above; the new kinds get a distinct glyph
// per spec §2.2 so the user can tell duplicate-detector firings apart
// from amount outliers at a glance.
const KIND_GLYPH_OVERRIDE: Partial<Record<AnomalyCard["kind"], { glyph: string; color: string }>> = {
  recurring_missing: { glyph: "🔁", color: "text-warning" },     // amber
  category_drift: { glyph: "🔀", color: "text-warning" },        // amber
  cross_card_duplicate: { glyph: "🚨", color: "text-rose-500" }, // rose
};

export function AnomalyHighlights({ anomalies }: AnomalyHighlightsProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Anomalies & alerts</CardTitle>
      </CardHeader>
      <CardContent>
        {anomalies.length === 0 ? (
          <div className="text-sm text-success py-4 text-center">
            ✓ All looks normal.
          </div>
        ) : (
          <ul className="flex flex-col gap-2">
            {anomalies.map((a, i) => {
              const Icon = ICON_BY_SEVERITY[a.severity];
              const kindOverride = KIND_GLYPH_OVERRIDE[a.kind];
              const clickable = Boolean(a.link);
              const inner = (
                <div
                  className={cn(
                    "flex gap-2 items-start p-2 rounded",
                    clickable
                      ? "hover:bg-secondary/60 cursor-pointer transition-colors"
                      : "hover:bg-secondary/40",
                  )}
                >
                  {kindOverride ? (
                    <span
                      className={cn(
                        "h-4 w-4 mt-0.5 shrink-0 text-sm leading-none",
                        kindOverride.color,
                      )}
                      aria-hidden="true"
                    >
                      {kindOverride.glyph}
                    </span>
                  ) : (
                    <Icon className={cn("h-4 w-4 mt-0.5 shrink-0", COLOR_BY_SEVERITY[a.severity])} />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">{a.message}</div>
                    {a.detail && (
                      <div className="text-xs text-muted-foreground mt-0.5">{a.detail}</div>
                    )}
                  </div>
                  {clickable && (
                    <ChevronRight
                      className="h-4 w-4 mt-0.5 shrink-0 text-muted-foreground"
                      aria-hidden="true"
                    />
                  )}
                </div>
              );
              return (
                <li key={i}>
                  {a.link ? <Link href={a.link}>{inner}</Link> : inner}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
