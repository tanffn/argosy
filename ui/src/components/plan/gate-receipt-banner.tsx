"use client";

import { AlertTriangle, CheckCircle2, XCircle } from "lucide-react";

import { Banner } from "@/components/ui/banner";
import type { GateReceiptDTO } from "@/lib/api";

interface GateReceiptBannerProps {
  receipt: GateReceiptDTO | null | undefined;
}

/**
 * Plan header chip that shows the verification receipt for the promotion
 * gates that ran during synthesis (task 0.2 / 0.10 — trust-restoration).
 *
 * Renders a one-line summary: "2/2 gates passed" (green) or
 * "1/2 gates passed; whole_artifact_reader DID_NOT_RUN (codex timeout)"
 * (warning).  When all gates passed, shows success tone; any non-pass
 * (BLOCK or DID_NOT_RUN) shows warning tone.
 *
 * Hidden when ``receipt`` is null/undefined (legacy drafts pre-dating
 * migration 0102, or synthesis runs that never reached gate evaluation).
 */
export function GateReceiptBanner({ receipt }: GateReceiptBannerProps) {
  if (!receipt) return null;

  const anyNonPass = receipt.gates.some((g) => g.status !== "pass");
  const hasBlock = receipt.gates.some((g) => g.status === "block");
  const tone = anyNonPass ? "warning" : "success";
  const Icon = hasBlock ? XCircle : anyNonPass ? AlertTriangle : CheckCircle2;

  return (
    <Banner
      tone={tone}
      icon={<Icon className="h-4 w-4" />}
      title="Promotion gate receipt"
      data-testid="gate-receipt-banner"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-mono">{receipt.summary}</span>
        {receipt.gates.length > 0 && (
          <details className="text-xs">
            <summary className="cursor-pointer font-medium underline-offset-4 hover:underline text-card-foreground">
              {receipt.gates.length} gate{receipt.gates.length !== 1 ? "s" : ""} →
            </summary>
            <ul className="mt-1 space-y-0.5 pl-2">
              {receipt.gates.map((g) => (
                <li key={g.gate} className="font-mono">
                  <span
                    className={
                      g.status === "pass"
                        ? "text-green-600 dark:text-green-400"
                        : g.status === "block"
                          ? "text-red-600 dark:text-red-400"
                          : "text-yellow-600 dark:text-yellow-400"
                    }
                  >
                    {g.status.toUpperCase()}
                  </span>{" "}
                  {g.gate}
                  {g.detail ? ` — ${g.detail}` : ""}
                  {g.override_by ? (
                    <span className="text-muted-foreground ml-1">
                      [overridden by {g.override_by}:{" "}
                      {g.override_reason ?? "no reason"}]
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </Banner>
  );
}
