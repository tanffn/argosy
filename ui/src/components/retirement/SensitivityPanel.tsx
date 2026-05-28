"use client";

import type { ValueWithRationale } from "@/lib/retirement-types";
import { ValueWithTooltip } from "./ValueWithTooltip";

export interface SensitivityLever {
  name: string;
  /** Signed: positive = increases verdict; negative = decreases. */
  delta_pp: number;
  direction: "up" | "down";
  source: ValueWithRationale;
}

interface Props {
  /** Levers will be sorted by absolute |delta_pp| descending. */
  levers: SensitivityLever[];
  /** Verdict unit, e.g. "pp" for percentage points or "%". Default "pp". */
  unit?: string;
}

/**
 * "Top levers — what moves the verdict most." Auto-renders the top 3
 * levers sorted by absolute effect. Each lever's rationale + source is
 * surfaced via the ValueWithTooltip primitive.
 *
 * Used inside <DrilldownSection title="Sensitivity">.
 */
export function SensitivityPanel({ levers, unit = "pp" }: Props) {
  const top3 = [...levers]
    .sort((a, b) => Math.abs(b.delta_pp) - Math.abs(a.delta_pp))
    .slice(0, 3);

  if (top3.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        Sensitivity analysis not available yet for this verdict.
      </p>
    );
  }

  return (
    <ul className="space-y-1.5 text-sm">
      {top3.map((l, i) => {
        const sign = l.delta_pp >= 0 ? "+" : "−";
        const color =
          l.delta_pp >= 0 ? "text-emerald-400" : "text-rose-400";
        return (
          <li
            key={l.name}
            className="flex items-baseline gap-2"
          >
            <span className="text-[10px] font-mono text-muted-foreground w-4">
              #{i + 1}
            </span>
            <ValueWithTooltip data={l.source}>{l.name}</ValueWithTooltip>
            <span className={`ml-auto font-mono ${color}`}>
              {sign}
              {Math.abs(l.delta_pp).toFixed(1)} {unit}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
