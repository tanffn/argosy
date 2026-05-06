/**
 * Minimal Dialog primitive — no Radix dependency, pure Tailwind + React.
 * Matches the project's "plain wrapper" component style (see card.tsx).
 *
 * API is intentionally compatible with the subset used by
 * distillate-edit-dialog.tsx so that it can be swapped for a Radix-
 * backed implementation later without touching callers.
 */
"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

interface DialogContextValue {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const DialogContext = React.createContext<DialogContextValue>({
  open: false,
  onOpenChange: () => undefined,
});

// ---------------------------------------------------------------------------
// Dialog root
// ---------------------------------------------------------------------------

interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
}

function Dialog({ open, onOpenChange, children }: DialogProps) {
  return (
    <DialogContext.Provider value={{ open, onOpenChange }}>
      {children}
    </DialogContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// DialogContent — renders as a modal overlay
// ---------------------------------------------------------------------------

function DialogContent({
  className,
  children,
  ...props
}: React.ComponentProps<"div">) {
  const { open, onOpenChange } = React.useContext(DialogContext);

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
        data-slot="dialog-content"
        className={cn(
          "fixed left-1/2 top-1/2 z-50 -translate-x-1/2 -translate-y-1/2",
          "w-full max-w-lg rounded-xl border bg-card text-card-foreground shadow-lg",
          "flex flex-col gap-4 p-6",
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
// DialogHeader / DialogFooter / DialogTitle / DialogDescription
// ---------------------------------------------------------------------------

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn("flex flex-col gap-1.5", className)}
      {...props}
    />
  );
}

function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn("flex items-center justify-end gap-2", className)}
      {...props}
    />
  );
}

function DialogTitle({ className, ...props }: React.ComponentProps<"h2">) {
  return (
    <h2
      data-slot="dialog-title"
      className={cn("text-base font-semibold leading-tight", className)}
      {...props}
    />
  );
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<"p">) {
  return (
    <p
      data-slot="dialog-description"
      className={cn("text-sm text-muted-foreground", className)}
      {...props}
    />
  );
}

export {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
};
