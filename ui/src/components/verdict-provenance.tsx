"use client";

/**
 * Verdict provenance strip — falsifier state + next-validation clock +
 * last fleet check. Shared by inbox trade/note cards, /positions cards,
 * and discovery watch picks (§7.1).
 *
 * ``none_recorded`` is a visible WARNING — never blank.
 */

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type FalsifierState = "armed" | "fired" | "none_recorded";

export interface VerdictProvenanceDTO {
  falsifier_state: FalsifierState | string;
  falsifiers?: string[];
  next_validation?: string | null;
  last_fleet_check_at?: string | null;
}

/** Relative past ("2d ago") or "just now". */
export function formatCheckedAgo(
  iso: string | null | undefined,
  nowMs: number = Date.now(),
): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const diffMs = nowMs - t;
  const sec = Math.max(0, Math.floor(diffMs / 1000));
  if (sec < 60) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

/** Relative future/past for the validation clock. */
export function formatValidationDue(
  isoDate: string | null | undefined,
  nowMs: number = Date.now(),
): string | null {
  if (!isoDate) return null;
  // Date-only strings parse as UTC midnight; compare on calendar days.
  const due = new Date(`${isoDate}T12:00:00Z`);
  if (Number.isNaN(due.getTime())) return null;
  const today = new Date(nowMs);
  const todayNoon = Date.UTC(
    today.getUTCFullYear(),
    today.getUTCMonth(),
    today.getUTCDate(),
    12,
  );
  const dayMs = 86_400_000;
  const delta = Math.round((due.getTime() - todayNoon) / dayMs);
  if (delta === 0) return "due today";
  if (delta > 0) return `due in ${delta}d`;
  return `overdue ${Math.abs(delta)}d`;
}

function stateLabel(state: string): string {
  switch (state) {
    case "armed":
      return "Falsifiers armed";
    case "fired":
      return "Falsifier fired — revisit unlocked";
    case "none_recorded":
    default:
      return "No falsifiers recorded";
  }
}

interface Props {
  provenance: VerdictProvenanceDTO;
  className?: string;
  /** Compact = single line for table/watch rows. */
  compact?: boolean;
}

export function VerdictProvenanceStrip({
  provenance,
  className,
  compact = false,
}: Props) {
  const state = provenance.falsifier_state || "none_recorded";
  const isWarning = state === "none_recorded";
  const isFired = state === "fired";
  const checked = formatCheckedAgo(provenance.last_fleet_check_at);
  const due = formatValidationDue(provenance.next_validation);
  const falsifiers = provenance.falsifiers ?? [];

  return (
    <div
      className={cn(
        "rounded-md border px-2.5 py-1.5 text-xs",
        isWarning
          ? "border-warning/50 bg-warning/10 text-warning-foreground"
          : isFired
            ? "border-info/40 bg-info/10"
            : "border-border/60 bg-muted/30",
        className,
      )}
      data-testid="verdict-provenance"
      data-falsifier-state={state}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge
          variant={isWarning ? "warning" : isFired ? "info" : "outline"}
          className="text-[10px]"
        >
          {isWarning ? "⚠ " : ""}
          {stateLabel(state)}
        </Badge>
        {checked && (
          <span className="text-muted-foreground">checked {checked}</span>
        )}
        {due && (
          <span className="text-muted-foreground">· validation {due}</span>
        )}
      </div>
      {!compact && falsifiers.length > 0 && (
        <ul className="mt-1 list-disc pl-4 text-muted-foreground space-y-0.5">
          {falsifiers.slice(0, 4).map((f) => (
            <li key={f}>{f}</li>
          ))}
          {falsifiers.length > 4 && (
            <li>+{falsifiers.length - 4} more</li>
          )}
        </ul>
      )}
    </div>
  );
}

/** Pull a provenance block out of an inbox body (nested under ``provenance``). */
export function provenanceFromBody(
  body: Record<string, unknown> | null | undefined,
): VerdictProvenanceDTO | null {
  if (!body || typeof body !== "object") return null;
  const raw = body.provenance;
  if (!raw || typeof raw !== "object") return null;
  const p = raw as Record<string, unknown>;
  const state = typeof p.falsifier_state === "string" ? p.falsifier_state : null;
  if (!state) return null;
  return {
    falsifier_state: state,
    falsifiers: Array.isArray(p.falsifiers)
      ? p.falsifiers.filter((x): x is string => typeof x === "string")
      : [],
    next_validation:
      typeof p.next_validation === "string" ? p.next_validation : null,
    last_fleet_check_at:
      typeof p.last_fleet_check_at === "string" ? p.last_fleet_check_at : null,
  };
}
