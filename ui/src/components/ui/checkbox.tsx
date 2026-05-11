/**
 * Checkbox primitive — plain Tailwind wrapper, matching project style (input.tsx, select.tsx).
 * Wraps a native <input type="checkbox"> with a consistent visual treatment.
 *
 * API surface is intentionally compatible with the shadcn Checkbox convention
 * (checked / onCheckedChange) so callers can be migrated to a Radix-backed
 * implementation later without changes.
 */
"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

interface CheckboxProps {
  checked?: boolean | "indeterminate";
  onCheckedChange?: (checked: boolean) => void;
  id?: string;
  disabled?: boolean;
  className?: string;
}

function Checkbox({
  checked,
  onCheckedChange,
  id,
  disabled,
  className,
}: CheckboxProps) {
  const isChecked = checked === true;
  const isIndeterminate = checked === "indeterminate";

  const ref = React.useRef<HTMLInputElement>(null);

  // Handle indeterminate state (only settable via DOM property)
  React.useEffect(() => {
    if (ref.current) {
      ref.current.indeterminate = isIndeterminate;
    }
  }, [isIndeterminate]);

  return (
    <input
      ref={ref}
      type="checkbox"
      id={id}
      checked={isChecked}
      disabled={disabled}
      onChange={(e) => onCheckedChange?.(e.target.checked)}
      className={cn(
        "h-4 w-4 rounded border border-input bg-background text-primary shadow-sm",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "cursor-pointer",
        className,
      )}
    />
  );
}

export { Checkbox };
