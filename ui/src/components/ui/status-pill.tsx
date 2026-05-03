import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const statusPillVariants = cva(
  "inline-flex items-center justify-center rounded-full border px-2 py-0.5 text-[10px] font-medium leading-tight whitespace-nowrap shrink-0 transition-colors",
  {
    variants: {
      tone: {
        success:
          "border-emerald-500/30 bg-emerald-500/10 text-emerald-400",
        warning:
          "border-amber-500/30 bg-amber-500/10 text-amber-400",
        error:
          "border-red-500/30 bg-red-500/10 text-red-400",
        neutral:
          "border-border bg-secondary/60 text-muted-foreground",
        accent:
          "border-cyan-500/30 bg-cyan-500/10 text-cyan-400",
      },
      mono: {
        true: "font-mono",
        false: "",
      },
    },
    defaultVariants: {
      tone: "neutral",
      mono: false,
    },
  },
);

export interface StatusPillProps
  extends Omit<React.ComponentProps<"span">, "color">,
    VariantProps<typeof statusPillVariants> {}

function StatusPill({
  className,
  tone,
  mono,
  ...props
}: StatusPillProps) {
  return (
    <span
      data-slot="status-pill"
      className={cn(statusPillVariants({ tone, mono }), className)}
      {...props}
    />
  );
}

export { StatusPill, statusPillVariants };
