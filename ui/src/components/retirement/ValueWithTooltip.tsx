"use client";

import { useState } from "react";
import type { ValueWithRationale } from "@/lib/retirement-types";

interface Props {
  /** Optional explicit display text. If omitted, formatted from `data`. */
  display?: string;
  /** Full citation metadata. */
  data: ValueWithRationale;
  /** Optional className for the trigger span. */
  className?: string;
  children?: React.ReactNode;
}

/**
 * Hover-explainable value. Renders the value as a subtle dotted-underline
 * span; on hover, a popover surfaces the rationale + source pointer.
 *
 * Used everywhere a retirement-related number appears in the UI. Pair with
 * <SourcesPanel/> for the deep-dive view at the bottom of the page.
 */
export function ValueWithTooltip({
  display,
  data,
  className,
  children,
}: Props) {
  const [open, setOpen] = useState(false);
  const shown = display ?? children ?? formatDefault(data);

  return (
    <span
      className={`relative inline-block border-b border-dotted border-muted-foreground/50 cursor-help ${className ?? ""}`}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      tabIndex={0}
      aria-describedby={open ? "vwt-popover" : undefined}
    >
      {shown}
      {open && (
        <span
          id="vwt-popover"
          role="tooltip"
          className="absolute left-1/2 -translate-x-1/2 top-full mt-1 z-50 w-72 rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-md text-left whitespace-normal"
        >
          <span className="block font-medium text-foreground">
            {shown}
            {data.unit ? ` ${data.unit}` : ""}
          </span>
          <span className="mt-1 block text-muted-foreground">
            {data.rationale}
          </span>
          {data.alternatives_considered &&
            data.alternatives_considered.length > 0 && (
              <span className="mt-1 block text-muted-foreground">
                <span className="font-medium">Alternatives:</span>{" "}
                {data.alternatives_considered.join(" · ")}
              </span>
            )}
          {data.freshness_warning && (
            <span className="mt-1 block text-amber-400">
              ⚠ {data.freshness_warning}
            </span>
          )}
          {data.source_id && (
            <a
              href={`#src-${data.source_id}`}
              className="mt-1 block text-[10px] opacity-70 font-mono hover:underline"
            >
              src: {data.source_id}
              {data.as_of_date ? ` · ${data.as_of_date}` : ""}
            </a>
          )}
        </span>
      )}
    </span>
  );
}

function formatDefault(d: ValueWithRationale): string {
  if (d.value === null || d.value === undefined) return "—";
  if (typeof d.value === "number") {
    if (d.unit === "fraction") return `${(d.value * 100).toFixed(1)}%`;
    if (d.unit === "NIS/mo") return `₪${d.value.toLocaleString()}`;
    if (d.unit === "USD") return `$${d.value.toLocaleString()}`;
    if (d.unit === "months") return `${d.value} months`;
    return d.value.toLocaleString();
  }
  return String(d.value);
}
