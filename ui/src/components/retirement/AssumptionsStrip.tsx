"use client";

import type { ValueWithRationale } from "@/lib/retirement-types";
import { ValueWithTooltip } from "./ValueWithTooltip";

export interface AssumptionSlider {
  id: string;
  label: string;
  source: ValueWithRationale;
  value: number;
  min: number;
  max: number;
  step: number;
  formatValue?: (v: number) => string;
  onChange: (v: number) => void;
}

interface Props {
  sliders: AssumptionSlider[];
  /** Optional reset-to-defaults handler. Shown only when supplied. */
  onReset?: () => void;
}

/**
 * Horizontal flex row of assumption sliders + reset. Each slider's label
 * is wrapped in <ValueWithTooltip> so the user can see the rationale +
 * source for the default value at a glance.
 */
export function AssumptionsStrip({ sliders, onReset }: Props) {
  return (
    <div className="mt-3 flex flex-wrap items-center gap-4 text-xs">
      {sliders.map((s) => (
        <label key={s.id} className="flex items-center gap-2">
          <ValueWithTooltip data={s.source}>
            <span className="text-muted-foreground">{s.label}</span>
          </ValueWithTooltip>
          <input
            type="range"
            min={s.min}
            max={s.max}
            step={s.step}
            value={s.value}
            onChange={(e) => s.onChange(Number(e.target.value))}
            className="w-32"
            aria-label={s.label}
          />
          <span className="font-mono min-w-[3rem] text-right">
            {s.formatValue ? s.formatValue(s.value) : s.value}
          </span>
        </label>
      ))}
      {onReset && (
        <button
          type="button"
          onClick={onReset}
          className="text-xs text-muted-foreground hover:text-foreground underline"
        >
          reset
        </button>
      )}
    </div>
  );
}
