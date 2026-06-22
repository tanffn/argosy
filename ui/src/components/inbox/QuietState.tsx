"use client";

/**
 * QuietState — the confident "you're all caught up" surface shown when the
 * inbox has nothing that needs the user. NOT a dead screen and NOT discovery
 * filler (that would turn "nothing needs you" into "go browse ideas" and
 * undermine the back-office trust contract). Just a calm confirmation plus
 * small liveness signals that Argosy is watching.
 */

import { Check, Eye } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import type { InboxLivenessDTO } from "@/lib/api";

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

export function QuietState({ liveness }: { liveness: InboxLivenessDTO }) {
  const signals: string[] = [];
  if (liveness.cash_within_band) signals.push("Cash within target band");
  if (liveness.no_overdue_tasks) signals.push("No overdue plan tasks");
  if (liveness.open_approvals === 0) signals.push("No open approvals");
  if (liveness.next_review) signals.push(`Next review ${fmtDate(liveness.next_review)}`);

  return (
    <Card className="border-border/60">
      <CardContent className="py-10 flex flex-col items-center gap-4 text-center">
        <div className="flex items-center justify-center h-11 w-11 rounded-full bg-success/15 text-success">
          <Eye className="h-5 w-5" aria-hidden />
        </div>
        <div className="space-y-1">
          <p className="text-base font-medium text-foreground">
            You&apos;re all caught up.
          </p>
          <p className="text-sm text-muted-foreground">
            Argosy is watching — nothing needs you right now.
          </p>
        </div>
        <ul className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
          {signals.map((s) => (
            <li key={s} className="inline-flex items-center gap-1.5">
              <Check className="h-3.5 w-3.5 text-success" aria-hidden />
              {s}
            </li>
          ))}
        </ul>
        <p className="text-[11px] text-muted-foreground/70">
          Last checked {fmtTime(liveness.last_checked)}
        </p>
      </CardContent>
    </Card>
  );
}
