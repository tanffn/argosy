"use client";

import { useState, type ReactNode } from "react";

/**
 * A titled section that collapses to a single header row. The header shows a
 * ``title`` plus an optional ``summary`` (e.g. "7 actions") so the user can see
 * what's inside without expanding. Collapsed by default; toggles on click with
 * an ``aria-expanded`` button for accessibility.
 *
 * Optionally CONTROLLED: pass ``open`` + ``onOpenChange`` when a parent
 * needs to drive the state (e.g. the home page's "Full detail →" button
 * expanding the demoted-sections region). When ``open`` is undefined the
 * component keeps its original uncontrolled behavior.
 */
export function CollapsibleSection({
  title,
  summary,
  defaultOpen = false,
  open: openProp,
  onOpenChange,
  children,
}: {
  title: string;
  summary?: string;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
}) {
  const [openState, setOpenState] = useState(defaultOpen);
  const open = openProp ?? openState;
  const toggle = () => {
    const next = !open;
    if (openProp === undefined) setOpenState(next);
    onOpenChange?.(next);
  };
  return (
    <div className="rounded-lg border border-border bg-background/40">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-4 py-3 text-left hover:bg-secondary/40"
      >
        <span className="flex items-center gap-2">
          <span aria-hidden className="text-muted-foreground">
            {open ? "▾" : "▸"}
          </span>
          <span className="font-mono font-semibold text-sm">{title}</span>
        </span>
        {summary && (
          <span className="text-xs text-muted-foreground">{summary}</span>
        )}
      </button>
      {open && <div className="px-4 pb-4 pt-1 space-y-4">{children}</div>}
    </div>
  );
}
