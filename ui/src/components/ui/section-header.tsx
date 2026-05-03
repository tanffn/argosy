import * as React from "react";

import { cn } from "@/lib/utils";

export interface SectionHeaderProps {
  label: string;
  count?: number;
  action?: React.ReactNode;
  className?: string;
}

function SectionHeader({
  label,
  count,
  action,
  className,
}: SectionHeaderProps) {
  return (
    <div
      data-slot="section-header"
      className={cn(
        "flex items-center justify-between gap-3 mb-3",
        className,
      )}
    >
      <div className="flex items-center gap-2">
        <span className="text-[11px] font-mono uppercase tracking-[0.14em] text-muted-foreground">
          {label}
        </span>
        {typeof count === "number" && (
          <span className="inline-flex items-center justify-center rounded-full bg-secondary/70 border border-border px-2 py-0.5 text-[10px] font-mono leading-none text-muted-foreground min-w-[1.25rem]">
            {count}
          </span>
        )}
      </div>
      {action ? <div className="flex items-center gap-2">{action}</div> : null}
    </div>
  );
}

export { SectionHeader };
