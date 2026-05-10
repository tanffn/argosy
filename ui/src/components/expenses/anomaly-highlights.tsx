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
  red: "text-rose-500",
  yellow: "text-amber-500",
  info: "text-sky-500",
} as const;

export function AnomalyHighlights({ anomalies }: AnomalyHighlightsProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Anomalies & alerts</CardTitle>
      </CardHeader>
      <CardContent>
        {anomalies.length === 0 ? (
          <div className="text-sm text-emerald-600 py-4 text-center">
            ✓ All looks normal.
          </div>
        ) : (
          <ul className="flex flex-col gap-2">
            {anomalies.map((a, i) => {
              const Icon = ICON_BY_SEVERITY[a.severity];
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
                  <Icon className={cn("h-4 w-4 mt-0.5 shrink-0", COLOR_BY_SEVERITY[a.severity])} />
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
