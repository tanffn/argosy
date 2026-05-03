"use client";

import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

export interface LiveClockProps {
  /** Render seconds (HH:MM:SS) when true, otherwise HH:MM. Default true. */
  seconds?: boolean;
  className?: string;
  /** Optional label rendered before the clock value, e.g. "Last updated". */
  label?: string;
}

function formatTime(date: Date, seconds: boolean): string {
  const h = String(date.getHours()).padStart(2, "0");
  const m = String(date.getMinutes()).padStart(2, "0");
  if (!seconds) return `${h}:${m}`;
  const s = String(date.getSeconds()).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

/**
 * Client-only live clock. Renders an empty placeholder during SSR / first
 * client paint to avoid hydration warnings, then ticks each second once
 * mounted. Use `seconds={false}` for a compact HH:MM variant (e.g. nav bar).
 */
export function LiveClock({
  seconds = true,
  className,
  label,
}: LiveClockProps) {
  const [now, setNow] = useState<Date | null>(null);

  useEffect(() => {
    // Kick the first tick to the next frame so we don't synchronously call
    // setState inside the effect body (which the React lint rule flags as a
    // cascading-render risk). The placeholder render is fine for one frame.
    let cancelled = false;
    const raf = window.requestAnimationFrame(() => {
      if (!cancelled) setNow(new Date());
    });
    const interval = window.setInterval(() => {
      setNow(new Date());
    }, 1000);
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(raf);
      window.clearInterval(interval);
    };
  }, []);

  const value = now ? formatTime(now, seconds) : "--:--" + (seconds ? ":--" : "");
  return (
    <span
      className={cn(
        "font-mono text-xs text-muted-foreground tabular-nums",
        className,
      )}
      // Suppress hydration warning here too — Dark Reader can mutate the
      // text node before our useEffect runs.
      suppressHydrationWarning
    >
      {label ? <span className="mr-1">{label}</span> : null}
      {value}
    </span>
  );
}
