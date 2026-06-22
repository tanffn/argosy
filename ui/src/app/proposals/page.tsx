"use client";

/**
 * Legacy /proposals → /inbox shim.
 *
 * The action hub was renamed from /proposals to /inbox (the IA is an inbox
 * now). This client-side shim preserves any query string and hash fragment
 * (e.g. /proposals#deploy-cash, /proposals#allocation) when redirecting, since
 * URL fragments never reach the server and so can't be redirected there. The
 * target anchors live on the /inbox page unchanged.
 */

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function ProposalsRedirect() {
  const router = useRouter();
  useEffect(() => {
    const { search, hash } = window.location;
    router.replace(`/inbox${search}${hash}`);
  }, [router]);
  return (
    <main className="max-w-4xl mx-auto p-6">
      <p className="text-sm text-muted-foreground">Taking you to your inbox…</p>
    </main>
  );
}
