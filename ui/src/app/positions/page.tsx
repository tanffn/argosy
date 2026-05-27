"use client";

import { PerPositionThesisSection } from "@/components/positions/per-position-thesis-section";

const USER_ID = "ariel";

/**
 * /positions — permalink for the per-position thesis cards. The same
 * cards are also rendered as a section inside /portfolio (which is the
 * primary home). This route is retained so older bookmarks / links keep
 * working.
 */
export default function PositionsPage() {
  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Positions</h1>
        <p className="text-sm text-muted-foreground">
          One card per holding · verdict + conviction + reasoning ·{" "}
          derived from the pending plan draft (or accepted plan when no
          draft is in flight).
        </p>
      </header>

      <PerPositionThesisSection userId={USER_ID} />
    </main>
  );
}
