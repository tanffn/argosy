"use client";

/**
 * Sprint commit #17 — Home Red-Flag Strip.
 *
 * Renders one row per active monitor_flags entry (allocation_drift /
 * mc_regression / macro_shift) at the top of the home page. Each row
 * carries a severity dot, a kind badge, a one-line summary derived
 * from the kind-specific payload, an Acknowledge (X) button, and a
 * deep link into the page that lets the user resolve the flag.
 *
 * Empty state is intentional — when there are no flags the component
 * returns null so the strip occupies zero vertical space (silent UX
 * is the right answer when nothing's wrong).
 *
 * Backend: argosy/services/plan_monitor.py +
 * argosy/api/routes/retirement.py GET /api/retirement/monitor/flags.
 * macro_shift writes haven't shipped yet (commit #15) — the strip
 * tolerates the empty union until then.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AlertCircle, AlertTriangle, Info, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  api,
  type MonitorFlagDTO,
  type MonitorFlagKind,
  type MonitorFlagSeverity,
} from "@/lib/api";

interface Props {
  userId: string;
}

export function RedFlagStrip({ userId }: Props) {
  const [flags, setFlags] = useState<MonitorFlagDTO[] | null>(null);
  const [pendingId, setPendingId] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .monitorFlags(userId)
      .then((rows) => {
        if (!cancelled) setFlags(rows);
      })
      .catch(() => {
        // Silent failure — the strip is non-critical chrome. If the
        // backend route is missing / 500s the rest of /home still
        // renders. Treat it as "no flags" so the strip stays invisible.
        if (!cancelled) setFlags([]);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const onAcknowledge = useCallback(
    async (flagId: number) => {
      if (flagId < 0) return; // synthetic id — backend route can't target it
      setPendingId(flagId);
      // Optimistic remove. If the backend POST fails we leave the row
      // dismissed locally (next page refresh will bring it back if the
      // acknowledge endpoint actually 404'd). Per the task spec the
      // backend route ships in a follow-on commit.
      setFlags((prev) => (prev ?? []).filter((f) => f.id !== flagId));
      try {
        await api.acknowledgeMonitorFlag(flagId);
      } catch {
        // Swallow — see comment above.
      } finally {
        setPendingId(null);
      }
    },
    [],
  );

  if (flags === null) return null; // initial fetch still in flight
  if (flags.length === 0) return null; // silent empty state

  return (
    <Card
      className="border-l-2 border-l-warning/60"
      data-slot="red-flag-strip"
    >
      <CardContent className="px-4 py-3 flex flex-col gap-2">
        <div className="text-[11px] font-mono uppercase tracking-wider text-muted-foreground">
          Red flags
        </div>
        <ul className="flex flex-col gap-2">
          {flags.map((flag) => (
            <RedFlagRow
              key={flag.id}
              flag={flag}
              busy={pendingId === flag.id}
              onAcknowledge={onAcknowledge}
            />
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

interface RedFlagRowProps {
  flag: MonitorFlagDTO;
  busy: boolean;
  onAcknowledge: (id: number) => void;
}

function RedFlagRow({ flag, busy, onAcknowledge }: RedFlagRowProps) {
  const dotClass = severityDotClass(flag.severity);
  const Icon = severityIcon(flag.severity);
  const summary = buildSummary(flag);
  const href = linkForKind(flag.kind);
  const canAcknowledge = flag.id >= 0;

  return (
    <li
      className="flex items-start gap-3 rounded-md border border-border bg-secondary/30 px-3 py-2"
      data-slot="red-flag-row"
    >
      <span
        aria-hidden
        className={`mt-1 inline-block h-2 w-2 shrink-0 rounded-full ${dotClass}`}
      />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center gap-2 flex-wrap">
          <Icon
            className={`h-3.5 w-3.5 shrink-0 ${severityTextClass(flag.severity)}`}
            aria-hidden
          />
          <Badge variant={severityBadgeVariant(flag.severity)} className="font-mono">
            {kindLabel(flag.kind)}
          </Badge>
          <span className="font-mono text-xs text-foreground">{summary}</span>
        </div>
        <Link
          href={href}
          className="font-mono text-[11px] text-info hover:underline"
        >
          View details -&gt;
        </Link>
      </div>
      <Button
        size="sm"
        variant="ghost"
        disabled={busy || !canAcknowledge}
        onClick={() => onAcknowledge(flag.id)}
        className="h-7 w-7 shrink-0 p-0"
        aria-label="Dismiss flag"
        title={
          canAcknowledge
            ? "Dismiss"
            : "Acknowledge unavailable — backend route pending"
        }
      >
        <X className="h-3.5 w-3.5" aria-hidden />
      </Button>
    </li>
  );
}

// ---------- helpers ---------------------------------------------------

function kindLabel(kind: MonitorFlagKind): string {
  switch (kind) {
    case "allocation_drift":
      return "Allocation drift";
    case "mc_regression":
      return "Monte Carlo regression";
    case "macro_shift":
      return "Macro shift";
  }
}

function linkForKind(kind: MonitorFlagKind): string {
  switch (kind) {
    case "allocation_drift":
      return "/proposals#allocation";
    case "mc_regression":
      return "/plan";
    case "macro_shift":
      return "/plan";
  }
}

function severityDotClass(severity: MonitorFlagSeverity): string {
  switch (severity) {
    case "critical":
      return "bg-rose-500";
    case "warning":
      return "bg-amber-500";
    case "info":
      return "bg-slate-400";
  }
}

function severityTextClass(severity: MonitorFlagSeverity): string {
  switch (severity) {
    case "critical":
      return "text-rose-500";
    case "warning":
      return "text-amber-500";
    case "info":
      return "text-slate-400";
  }
}

function severityIcon(
  severity: MonitorFlagSeverity,
): React.ComponentType<{ className?: string }> {
  switch (severity) {
    case "critical":
      return AlertTriangle;
    case "warning":
      return AlertCircle;
    case "info":
      return Info;
  }
}

function severityBadgeVariant(
  severity: MonitorFlagSeverity,
): "error" | "warning" | "secondary" {
  switch (severity) {
    case "critical":
      return "error";
    case "warning":
      return "warning";
    case "info":
      return "secondary";
  }
}

function buildSummary(flag: MonitorFlagDTO): string {
  try {
    switch (flag.kind) {
      case "allocation_drift":
        return driftSummary(flag.payload);
      case "mc_regression":
        return mcRegressionSummary(flag.payload);
      case "macro_shift":
        return macroShiftSummary(flag.payload);
    }
  } catch {
    return `${kindLabel(flag.kind)} flag detected`;
  }
}

function driftSummary(payload: Record<string, unknown>): string {
  const row = typeof payload.row_category === "string" ? payload.row_category : null;
  const relDrift =
    typeof payload.rel_drift === "number" ? payload.rel_drift : null;
  // The /monitor/flags response from AllocationDriftFlag.to_dict() does
  // NOT include the per-row current_pct + target_pct snapshot values
  // (only the rel_drift fraction + abs_drift_usd). We fall back to the
  // most informative shape we can build, preferring abs_drift_usd when
  // present so the user gets a dollar anchor.
  const absDriftUsd =
    typeof payload.abs_drift_usd === "number" ? payload.abs_drift_usd : null;

  if (row === null && relDrift === null && absDriftUsd === null) {
    return "Allocation drift flag detected";
  }

  const parts: string[] = [];
  if (row) parts.push(row);
  if (relDrift !== null) parts.push(`${(relDrift * 100).toFixed(1)}% drift`);
  if (absDriftUsd !== null) parts.push(`$${(absDriftUsd / 1000).toFixed(0)}K gap`);
  return parts.length > 0 ? parts.join(" · ") : "Allocation drift flag detected";
}

function mcRegressionSummary(payload: Record<string, unknown>): string {
  const prev =
    typeof payload.prev_p_solvent === "number" ? payload.prev_p_solvent : null;
  const curr =
    typeof payload.curr_p_solvent === "number" ? payload.curr_p_solvent : null;
  const delta =
    typeof payload.delta_pp === "number" ? payload.delta_pp : null;
  if (prev === null || curr === null) {
    return "Monte Carlo regression flag detected";
  }
  // P(solvent) values arrive as fractions (0..1) from the backend
  // dataclass. Render them as percentages so the row is immediately
  // scannable; delta_pp is already in percentage-point units.
  const prevPct = (prev * 100).toFixed(0);
  const currPct = (curr * 100).toFixed(0);
  const deltaStr =
    delta === null
      ? ""
      : ` (${delta >= 0 ? "+" : ""}${delta.toFixed(1)}pp)`;
  return `P(solvent) ${prevPct}% -> ${currPct}%${deltaStr}`;
}

function macroShiftSummary(payload: Record<string, unknown>): string {
  const trigger =
    typeof payload.trigger === "string" ? payload.trigger : null;
  const rationale =
    typeof payload.classifier_rationale === "string"
      ? payload.classifier_rationale
      : null;
  if (trigger === null && rationale === null) {
    return "Macro-shift signal detected";
  }
  const snippet =
    rationale === null
      ? null
      : rationale.length > 80
        ? `${rationale.slice(0, 77).trimEnd()}...`
        : rationale;
  if (trigger && snippet) return `${trigger}: ${snippet}`;
  if (trigger) return trigger;
  return snippet ?? "Macro-shift signal detected";
}
