import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center justify-center rounded-md border px-2 py-0.5 text-xs font-medium w-fit whitespace-nowrap shrink-0 [&>svg]:size-3 gap-1 [&>svg]:pointer-events-none focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:ring-[3px] aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 aria-invalid:border-destructive transition-colors duration-200 overflow-hidden",
  {
    variants: {
      // All semantic variants share one recipe — `/10 bg + /30 border +
      // full-strength text`. Same shape, three (now five) colors. Drops
      // the old solid-fill variants that fought with `StatusPill` and
      // glowed too brightly against `--card`.
      variant: {
        default:
          "border-border bg-secondary text-secondary-foreground [a&]:hover:bg-secondary/70",
        secondary:
          "border-border bg-secondary/60 text-secondary-foreground [a&]:hover:bg-secondary/80",
        destructive:
          "border-error/30 bg-error/10 text-error [a&]:hover:bg-error/15 focus-visible:ring-error/30",
        outline:
          "text-foreground [a&]:hover:bg-accent [a&]:hover:text-accent-foreground",
        success:
          "border-success/30 bg-success/10 text-success [a&]:hover:bg-success/15",
        warning:
          "border-warning/30 bg-warning/10 text-warning [a&]:hover:bg-warning/15",
        error:
          "border-error/30 bg-error/10 text-error [a&]:hover:bg-error/15",
        info:
          "border-info/30 bg-info/10 text-info [a&]:hover:bg-info/15",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return (
    <span
      data-slot="badge"
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  );
}

export { Badge, badgeVariants };
