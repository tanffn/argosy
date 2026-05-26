"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { FMObjection } from "@/lib/api";

interface FMObjectionsCardProps {
  objections: FMObjection[];
  // When provided, renders a primary CTA below the list that re-synthesizes
  // the plan with the Fund Manager's objections fed back to the fleet as
  // guidance. The caller wires this to /api/advisor/check-in with the
  // formatted objection text.
  onResynthesize?: () => void | Promise<void>;
  resynthesizing?: boolean;
}

function severityClasses(s: FMObjection["severity"]) {
  switch (s) {
    case "RED":
      return {
        badge: "error" as const,
        dot: "bg-error",
        ring: "border-error/40 bg-error/5",
      };
    case "AMBER":
      return {
        badge: "secondary" as const,
        dot: "bg-warning",
        ring: "border-warning/40 bg-warning/5",
      };
    case "YELLOW":
    default:
      return {
        badge: "outline" as const,
        dot: "bg-muted-foreground",
        ring: "border-border/60 bg-muted/20",
      };
  }
}

export function FMObjectionsCard(props: FMObjectionsCardProps) {
  const { objections, onResynthesize, resynthesizing } = props;
  if (objections.length === 0) return null;

  // Sort RED → AMBER → YELLOW so the most-critical concerns sit on top.
  const sevOrder: Record<FMObjection["severity"], number> = {
    RED: 0,
    AMBER: 1,
    YELLOW: 2,
  };
  const sorted = [...objections].sort(
    (a, b) => sevOrder[a.severity] - sevOrder[b.severity],
  );

  return (
    <div className="rounded-md border border-error/40 bg-error/5 p-4">
      <div className="flex items-center gap-2 mb-3">
        <AlertTriangle className="h-4 w-4 text-error" />
        <h3 className="text-sm font-semibold tracking-wide uppercase text-error">
          Fund Manager objections ({objections.length})
        </h3>
        <span className="ml-2 text-[10px] font-mono text-muted-foreground">
          (the agent that signs off on the synthesized plan)
        </span>
      </div>
      <ul className="flex flex-col gap-2">
        {sorted.map((o, i) => {
          const cls = severityClasses(o.severity);
          return (
            <li
              key={i}
              className={`rounded-md border ${cls.ring} p-3 text-sm`}
            >
              <div className="flex items-start gap-2">
                <span
                  className={`mt-1.5 h-2 w-2 rounded-full ${cls.dot} flex-shrink-0`}
                  aria-hidden
                />
                <div className="flex-1">
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="font-medium">{o.topic}</span>
                    <Badge variant={cls.badge}>{o.severity}</Badge>
                  </div>
                  <p className="text-sm text-muted-foreground">{o.detail}</p>
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      {onResynthesize && (
        <div className="mt-3 pt-3 border-t border-error/30 flex items-center justify-between gap-3 flex-wrap">
          <p className="text-xs text-muted-foreground">
            Don&apos;t want to handle these yourself? Send the objections back
            to the fleet — the analysts and synthesizer re-run with the Fund
            Manager&apos;s concerns as explicit guidance.
          </p>
          <Button
            onClick={onResynthesize}
            disabled={resynthesizing}
            variant="default"
            size="sm"
            className="whitespace-nowrap"
          >
            <RefreshCw
              className={`h-3.5 w-3.5 mr-1 ${
                resynthesizing ? "animate-spin" : ""
              }`}
            />
            {resynthesizing
              ? "Re-synthesizing…"
              : "Re-synthesize addressing concerns"}
          </Button>
        </div>
      )}
    </div>
  );
}
