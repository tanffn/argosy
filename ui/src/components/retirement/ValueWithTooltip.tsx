"use client";

import { useCallback, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ValueWithRationale } from "@/lib/retirement-types";

// Popover width must match the Tailwind class below (w-72 = 18rem = 288px)
// so the edge-aware clamp uses the real rendered width.
const POPOVER_WIDTH = 288;
const VIEWPORT_MARGIN = 8;

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
  const triggerRef = useRef<HTMLSpanElement | null>(null);
  // null = closed. When open, we hold the FIXED viewport coordinates of
  // the popover so it can escape any `overflow-hidden` ancestor (e.g. the
  // HeroCard, which clips an absolutely-positioned child). Computed from
  // the trigger's bounding rect in the event handler — never in an effect
  // body, per the project's no-sync-setState-in-effect rule.
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const shown = display ?? children ?? formatDefault(data);

  const openAt = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    // Center under the trigger, then clamp to the viewport so the full
    // text is always visible — never clipped by a card or the window edge.
    const maxLeft = window.innerWidth - POPOVER_WIDTH - VIEWPORT_MARGIN;
    const centered = r.left + r.width / 2 - POPOVER_WIDTH / 2;
    const left = Math.max(VIEWPORT_MARGIN, Math.min(centered, maxLeft));
    setPos({ left, top: r.bottom + 4 });
  }, []);

  const close = useCallback(() => setPos(null), []);

  const open = pos !== null;

  return (
    <span
      ref={triggerRef}
      className={`relative inline-block border-b border-dotted border-muted-foreground/50 cursor-help ${className ?? ""}`}
      onMouseEnter={openAt}
      onMouseLeave={close}
      onFocus={openAt}
      onBlur={close}
      tabIndex={0}
      aria-describedby={open ? "vwt-popover" : undefined}
    >
      {shown}
      {open &&
        typeof document !== "undefined" &&
        createPortal(
          <span
            id="vwt-popover"
            role="tooltip"
            style={{ position: "fixed", left: pos.left, top: pos.top }}
            className="z-50 w-72 max-w-[calc(100vw-16px)] rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-md text-left whitespace-normal"
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
          </span>,
          document.body,
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
