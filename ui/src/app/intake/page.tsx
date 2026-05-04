"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * Legacy /intake page — redirects to the reframed /advisor panel.
 *
 * The 6-stage interview was replaced by a persistent advisor relationship
 * (gap-tracker sidebar + free-form Q&A) per Phase 1 reframe. This stub
 * stays in place so any inbound link or browser bookmark to /intake
 * still lands on the right surface.
 */
export default function IntakeRedirect() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/advisor");
  }, [router]);

  return (
    <main className="max-w-3xl mx-auto p-6">
      <p className="text-sm text-muted-foreground">
        Redirecting to the Advisor panel...
      </p>
    </main>
  );
}
