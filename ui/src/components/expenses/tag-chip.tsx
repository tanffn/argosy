"use client";

import Link from "next/link";

import { cn } from "@/lib/utils";

interface TagChipProps {
  tag: string;
  onRemove?: () => void;
  href?: string;          // optional link target (e.g. trips page)
  className?: string;
}

/**
 * Color-coded tag chip. Tags prefixed with `trip:` render in blue;
 * generic tags render in muted/gray. Optional remove button + link wrap.
 */
export function TagChip({ tag, onRemove, href, className }: TagChipProps) {
  const isTrip = tag.startsWith("trip:");
  const display = isTrip ? tag.slice("trip:".length) : tag;
  const baseStyle = isTrip
    ? "bg-info/10 text-info border border-info/30"
    : "bg-secondary text-secondary-foreground border border-border";
  const inner = (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
        baseStyle,
        className,
      )}
    >
      {isTrip && <span className="text-[10px] opacity-70">✈</span>}
      <span>{display}</span>
      {onRemove && (
        <button
          type="button"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onRemove();
          }}
          className="ml-0.5 hover:text-error transition-colors duration-200"
          aria-label={`Remove ${tag}`}
        >
          ×
        </button>
      )}
    </span>
  );
  if (href) {
    return <Link href={href}>{inner}</Link>;
  }
  return inner;
}
