"use client";

import { AlertTriangle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { FMObjection } from "@/lib/api";

interface FMObjectionsCardProps {
  objections: FMObjection[];
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
  const { objections } = props;
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
    </div>
  );
}
