"use client";

// Sprint A commit #8 — /admin/jobs page.
//
// Client component because:
//   1. The admin-token gate reads localStorage (browser-only).
//   2. The jobs table polls /api/jobs every 5 s while visible.
// Both rule out the default server-component fetch pattern. Other
// project pages that mutate state (e.g. settings/page.tsx) use the
// same `"use client"` shape.

import { AdminTokenGate } from "@/components/admin/jobs/AdminTokenGate";
import { JobsTable } from "@/components/admin/jobs/JobsTable";

export default function AdminJobsPage() {
  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Jobs</h1>
        <p className="text-sm text-muted-foreground">
          Cadence loops + long-running jobs from the registry. Run-now,
          stop, and reconnect are admin-gated; GET routes (list, view,
          history) are open. Polls every 5 s while the tab is focused.
        </p>
      </header>

      <AdminTokenGate>
        <JobsTable />
      </AdminTokenGate>
    </main>
  );
}
