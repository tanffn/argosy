"use client";

import { useCallback, useState } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";

interface ExportPlanButtonProps {
  userId: string;
  variant?: "default" | "outline" | "ghost";
  size?: "default" | "sm" | "lg" | "icon";
  label?: string;
}

/**
 * Small "Export plan as markdown" button. Fetches the live one-pager
 * from ``GET /api/plan/export``, wraps the response body in a Blob, and
 * triggers a browser download as ``argosy-plan-YYYY-MM-DD.md``.
 *
 * Shared by ``/plan`` and ``/portfolio`` headers so the markdown export
 * is always one click away from where the user is reading their state.
 *
 * No external dependencies: uses the standard ``URL.createObjectURL`` +
 * anchor-with-download-attr pattern. The anchor is detached from the
 * DOM immediately after click; the object URL is revoked on a short
 * timeout to give Safari a chance to finish the download.
 */
export function ExportPlanButton({
  userId,
  variant = "outline",
  size = "sm",
  label = "Export plan as markdown",
}: ExportPlanButtonProps) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onClick = useCallback(async () => {
    setError(null);
    setWorking(true);
    try {
      const body = await api.planExportMarkdown(userId);
      const today = new Date().toISOString().slice(0, 10);
      const filename = `argosy-plan-${today}.md`;
      const blob = new Blob([body], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      // Append so Firefox honors the download attr; detach immediately.
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // Give the browser a moment to start the download before revoking.
      setTimeout(() => URL.revokeObjectURL(url), 250);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWorking(false);
    }
  }, [userId]);

  return (
    <div className="flex items-center gap-2">
      <Button
        variant={variant}
        size={size}
        onClick={onClick}
        disabled={working}
        title="Download a one-page markdown snapshot of the plan + wealth dashboard"
      >
        {working ? "Exporting…" : label}
      </Button>
      {error && (
        <span className="text-xs text-error font-mono">{error}</span>
      )}
    </div>
  );
}
