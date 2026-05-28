"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

interface Props {
  title: string;
  defaultOpen?: boolean;
  badge?: string;
  children: React.ReactNode;
}

/**
 * Collapsible "drill-down" section. Used for Methodology / Sensitivity /
 * Sources panels — the bottom-half of every retirement-relevant page.
 *
 * Visual contract: chevron + title in a single clickable row; expanded
 * content inset with a thin left border to signal hierarchy. Matches the
 * §0.1 viz standard from the master plan.
 */
export function DrilldownSection({
  title,
  defaultOpen = false,
  badge,
  children,
}: Props) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="mt-4">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
        <span>{title}</span>
        {badge && (
          <span className="ml-1 rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono">
            {badge}
          </span>
        )}
      </button>
      {open && (
        <div className="mt-2 ml-2 border-l-2 border-border/40 pl-3 text-sm">
          {children}
        </div>
      )}
    </div>
  );
}
