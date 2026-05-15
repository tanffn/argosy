import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const statusPillVariants = cva(
  "inline-flex items-center justify-center rounded-full border px-2 py-0.5 text-[10px] font-medium leading-tight whitespace-nowrap shrink-0 transition-colors duration-200",
  {
    variants: {
      tone: {
        success:
          "border-success/30 bg-success/10 text-success",
        warning:
          "border-warning/30 bg-warning/10 text-warning",
        error:
          "border-error/30 bg-error/10 text-error",
        neutral:
          "border-border bg-secondary/60 text-muted-foreground",
        accent:
          "border-info/30 bg-info/10 text-info",
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
