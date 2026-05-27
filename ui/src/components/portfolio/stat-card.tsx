"use client";

import * as React from "react";

import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * StatCard — small wealth-dashboard tile (used by Row 2 + Row 3 in
 * the /portfolio top-of-page grid).
 *
 * Structure:
 *   - eyebrow (uppercase label)
 *   - value (large mono number)
 *   - subline (small text, e.g. "vs target 45%")
 *   - visual (free-form children: bar, gauge, donut, etc.)
 *   - tooltip (rendered as a small "?" with a title attribute when
 *     ``missingReasons`` is non-empty; surfaces graceful-degradation
 *     state without occupying chart real estate)
 *
 * Color tokens follow the existing palette in
 * ``ui/src/app/globals.css`` — text-success / text-warning / text-error /
 * text-muted-foreground. No new design tokens are introduced here.
 */
export interface StatCardProps {
  eyebrow: string;
  value: React.ReactNode;
  subline?: React.ReactNode;
  tone?: "default" | "success" | "warning" | "error";
  missingReasons?: string[];
  children?: React.ReactNode;
  className?: string;
}

export function StatCard({
  eyebrow,
  value,
  subline,
  tone = "default",
  missingReasons,
  children,
  className,
}: StatCardProps) {
  const toneClass =
    tone === "success"
      ? "text-success"
      : tone === "warning"
        ? "text-warning"
        : tone === "error"
          ? "text-error"
          : "text-foreground";

  const hasMissing = missingReasons && missingReasons.length > 0;

  return (
    <Card className={cn("py-4", className)}>
      <CardContent className="px-4 flex flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
            {eyebrow}
          </div>
          {hasMissing && (
            <span
              className="inline-flex h-4 w-4 items-center justify-center rounded-full bg-muted text-[10px] text-muted-foreground cursor-help"
              title={missingReasons.join("\n")}
              aria-label="missing data"
            >
              ?
            </span>
          )}
        </div>
        <div className={cn("text-2xl font-mono font-semibold", toneClass)}>
          {value}
        </div>
        {subline && (
          <div className="text-xs text-muted-foreground">{subline}</div>
        )}
        {children && <div className="mt-1">{children}</div>}
      </CardContent>
    </Card>
  );
}

/**
 * Common helper: format a NIS number with thousands separators + an
 * optional compact suffix (k / M) — keeps the headline number short.
 */
export function formatNis(n: number | null | undefined): string {
  if (n == null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toFixed(0);
}

export function formatUsd(n: number | null | undefined): string {
  if (n == null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `$${(n / 1_000).toFixed(1)}k`;
  return `$${n.toFixed(0)}`;
}

export function formatPct(
  n: number | null | undefined,
  decimals = 1,
): string {
  if (n == null) return "—";
  return `${n.toFixed(decimals)}%`;
}
