import * as React from "react";

import { cn } from "@/lib/utils";

function Card({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card"
      className={cn(
        // Card hover micro-interaction (refined toward the agatha-service
        // aesthetic): 1px upward translate + a soft accent-tinted shadow
        // + border brightening. 250ms ease-out is fast enough to feel
        // responsive, slow enough to read as deliberate. The shadow uses
        // negative spread so the glow stays close to the card edge and
        // doesn't muddy the page background.
        "bg-card text-card-foreground flex flex-col gap-6 rounded-lg border py-6",
        "transition-[transform,border-color,box-shadow] duration-200 ease-out",
        "hover:-translate-y-px hover:border-foreground/25 hover:shadow-[0_8px_24px_-12px_rgb(120_140_220/0.18)]",
        className,
      )}
      {...props}
    />
  );
}

function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-header"
      className={cn(
        "@container/card-header grid auto-rows-min grid-rows-[auto_auto] items-start gap-1.5 px-6 has-data-[slot=card-action]:grid-cols-[1fr_auto] [.border-b]:pb-6",
        className,
      )}
      {...props}
    />
  );
}

function CardTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-title"
      className={cn("leading-none font-semibold", className)}
      {...props}
    />
  );
}

function CardDescription({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-description"
      className={cn("text-muted-foreground text-sm", className)}
      {...props}
    />
  );
}

function CardAction({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-action"
      className={cn(
        "col-start-2 row-span-2 row-start-1 self-start justify-self-end",
        className,
      )}
      {...props}
    />
  );
}

function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-content"
      className={cn("px-6", className)}
      {...props}
    />
  );
}

function CardFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="card-footer"
      className={cn("flex items-center px-6 [.border-t]:pt-6", className)}
      {...props}
    />
  );
}

/**
 * CardIcon — the rounded-square brand-icon slot at a feature card's
 * top-left, matching the agatha-service screenshot's
 * icon-then-title-then-body vertical stack. Pass a small lucide icon
 * as `children`; the wrapper supplies the tinted rounded square. Tone
 * defaults to `info`, but any semantic token works.
 */
function CardIcon({
  className,
  tone = "info",
  children,
  ...props
}: React.ComponentProps<"span"> & {
  tone?: "success" | "warning" | "error" | "info" | "neutral";
}) {
  const toneClass = {
    success: "bg-success/15 text-success border-success/20",
    warning: "bg-warning/15 text-warning border-warning/20",
    error: "bg-error/15 text-error border-error/20",
    info: "bg-info/15 text-info border-info/20",
    neutral: "bg-secondary text-foreground border-border",
  }[tone];
  return (
    <span
      data-slot="card-icon"
      className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-md border [&_svg]:h-4 [&_svg]:w-4",
        toneClass,
        className,
      )}
      aria-hidden
      suppressHydrationWarning
      {...props}
    >
      {children}
    </span>
  );
}

export {
  Card,
  CardHeader,
  CardFooter,
  CardTitle,
  CardAction,
  CardDescription,
  CardContent,
  CardIcon,
};
