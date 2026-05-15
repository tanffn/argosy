import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * Inline notice banner — same translucent recipe family as StatusPill
 * (border-X/30 + bg-X/10 + text-X) but sized for a full-width
 * announcement with an icon, title, and body content. Modeled on the
 * agatha-service "Limited preview" banner: subtle tint, clear hierarchy,
 * no flash. Use for in-page "heads up" content (preview gates, missing
 * data, scheduled maintenance, etc.).
 *
 * Composition:
 *   <Banner tone="warning" icon={<AlertTriangle />} title="Limited preview">
 *     Body content — paragraphs, bullets, links, whatever.
 *   </Banner>
 */
const bannerVariants = cva(
  "rounded-lg border px-4 py-3 flex gap-3 items-start transition-colors duration-200",
  {
    variants: {
      tone: {
        success: "border-success/30 bg-success/10",
        warning: "border-warning/30 bg-warning/10",
        error: "border-error/30 bg-error/10",
        info: "border-info/30 bg-info/10",
        neutral: "border-border bg-card",
      },
    },
    defaultVariants: { tone: "info" },
  },
);

const iconToneVariants = cva("shrink-0 mt-0.5 inline-flex items-center justify-center rounded-md h-7 w-7", {
  variants: {
    tone: {
      success: "bg-success/15 text-success",
      warning: "bg-warning/15 text-warning",
      error: "bg-error/15 text-error",
      info: "bg-info/15 text-info",
      neutral: "bg-secondary text-foreground",
    },
  },
  defaultVariants: { tone: "info" },
});

const titleToneVariants = cva("font-medium text-sm leading-tight", {
  variants: {
    tone: {
      success: "text-success",
      warning: "text-warning",
      error: "text-error",
      info: "text-info",
      neutral: "text-foreground",
    },
  },
  defaultVariants: { tone: "info" },
});

export interface BannerProps
  extends Omit<React.ComponentProps<"div">, "title">,
    VariantProps<typeof bannerVariants> {
  icon?: React.ReactNode;
  title?: React.ReactNode;
}

export function Banner({
  className,
  tone,
  icon,
  title,
  children,
  ...props
}: BannerProps) {
  return (
    <div
      data-slot="banner"
      className={cn(bannerVariants({ tone }), className)}
      {...props}
    >
      {icon ? (
        <span className={iconToneVariants({ tone })} aria-hidden suppressHydrationWarning>
          {icon}
        </span>
      ) : null}
      <div className="min-w-0 flex-1 text-sm text-card-foreground">
        {title ? <div className={titleToneVariants({ tone })}>{title}</div> : null}
        <div className={cn(title ? "mt-1" : "", "text-muted-foreground")}>
          {children}
        </div>
      </div>
    </div>
  );
}
