/**
 * Minimal Sheet primitive — a side-drawer variant of Dialog. No Radix
 * dependency, pure Tailwind + React. Mirrors the shadcn API shape
 * (Sheet / SheetContent / SheetHeader / SheetTitle / SheetDescription)
 * so call sites can move to a Radix-backed implementation later without
 * changes.
 *
 * `SheetContent` anchors to the right edge. Add side="left" back here
 * if a future caller needs it (YAGNI).
 */
"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

interface SheetContextValue {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const SheetContext = React.createContext<SheetContextValue>({
  open: false,
  onOpenChange: () => undefined,
});

// ---------------------------------------------------------------------------
// Sheet root
// ---------------------------------------------------------------------------

interface SheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
}

function Sheet({ open, onOpenChange, children }: SheetProps) {
  return (
    <SheetContext.Provider value={{ open, onOpenChange }}>
      {children}
    </SheetContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// SheetContent — renders as a side-drawer overlay
// ---------------------------------------------------------------------------

// W10 — empty interface extending a supertype is forbidden by
// @typescript-eslint/no-empty-object-type. Type-alias keeps the shape
// identical (every supertype member is preserved) and is the
// idiomatic shadcn-style declaration for "no additional props yet".
type SheetContentProps = React.ComponentProps<"div">;

function SheetContent({
  className,
  children,
  ...props
}: SheetContentProps) {
  const { open, onOpenChange } = React.useContext(SheetContext);

  // Close on Escape.
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onOpenChange(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  if (!open) return null;

  return (
    <>
      {/* Backdrop */}
      <div
        aria-hidden
        className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm"
        onClick={() => onOpenChange(false)}
      />
      {/* Panel */}
      <div
        role="dialog"
        aria-modal="true"
        data-slot="sheet-content"
        className={cn(
          "fixed top-0 bottom-0 right-0 z-50 flex flex-col gap-4 bg-card text-card-foreground shadow-lg border-l p-6",
          className,
        )}
        {...props}
      >
        {children}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// SheetHeader / SheetTitle / SheetDescription
// ---------------------------------------------------------------------------

function SheetHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sheet-header"
      className={cn("flex flex-col gap-1.5", className)}
      {...props}
    />
  );
}

function SheetTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return (
    <h2
      data-slot="sheet-title"
      className={cn("text-base font-semibold leading-tight", className)}
      {...props}
    />
  );
}

function SheetDescription({
  className,
  ...props
}: React.ComponentProps<"p">) {
  return (
    <p
      data-slot="sheet-description"
      className={cn("text-sm text-muted-foreground", className)}
      {...props}
    />
  );
}

export { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle };
