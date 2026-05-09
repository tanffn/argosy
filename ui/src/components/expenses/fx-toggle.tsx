"use client";

import { useFxMode, type FxMode } from "@/lib/expenses/fx-mode";
import { cn } from "@/lib/utils";

export function FxToggle({ className }: { className?: string }) {
  const [mode, setMode] = useFxMode();
  const opts: { value: FxMode; label: string }[] = [
    { value: "per_currency", label: "Per currency" },
    { value: "nis", label: "NIS-converted" },
  ];
  return (
    <div className={cn(
      "inline-flex items-center rounded-md border border-border bg-background p-0.5 text-xs",
      className,
    )}>
      {opts.map((o) => (
        <button
          key={o.value}
          onClick={() => setMode(o.value)}
          className={cn(
            "px-2.5 py-1 rounded transition-colors",
            mode === o.value
              ? "bg-secondary text-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
