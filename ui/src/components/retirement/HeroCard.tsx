"use client";

import { Card } from "@/components/ui/card";

type Status = "ON_TRACK" | "WARN" | "OFF_TRACK" | "UNCERTAIN";

interface KeyValue {
  label: string;
  /** Pre-formatted display value (e.g. "87%", "₪2,100/mo", "51 years"). */
  display: string;
  /** Optional secondary text (e.g. "was 49"). */
  secondary?: string;
  /** Optional tooltip / children slot (e.g. wrap in <ValueWithTooltip>). */
  children?: React.ReactNode;
}

interface Props {
  title: string;
  status: Status;
  /**
   * Optional override for the status chip text. The status still drives the
   * color (dot/bar/text), but some surfaces want a domain word instead of
   * the readiness vocabulary — e.g. a reconciled cash event reads
   * "RECONCILED", not "ON TRACK". Defaults to the status's standard label.
   */
  statusLabel?: string;
  /** One-line verdict copy (e.g. "P(solvent at 95) above target"). */
  verdict?: string;
  /** 1-3 primary numbers. */
  numbers: KeyValue[];
  /** Optional explanatory subline. */
  subline?: React.ReactNode;
}

const STATUS_STYLE: Record<Status, { dot: string; text: string; bar: string }> = {
  ON_TRACK: { dot: "bg-emerald-500", text: "text-emerald-400", bar: "from-emerald-500/15 to-transparent" },
  WARN: { dot: "bg-amber-500", text: "text-amber-400", bar: "from-amber-500/15 to-transparent" },
  OFF_TRACK: { dot: "bg-rose-500", text: "text-rose-400", bar: "from-rose-500/15 to-transparent" },
  UNCERTAIN: { dot: "bg-slate-400", text: "text-slate-300", bar: "from-slate-500/15 to-transparent" },
};

const STATUS_LABEL: Record<Status, string> = {
  ON_TRACK: "ON TRACK",
  WARN: "WARN",
  OFF_TRACK: "OFF TRACK",
  UNCERTAIN: "UNCERTAIN",
};

/**
 * Hero card — top-of-page verdict surface. The §0.1 viz standard.
 *
 * Renders: status dot + title + verdict + 1-3 key numbers.
 *
 * Typical use:
 *   <HeroCard
 *     title="Retirement readiness"
 *     status="WARN"
 *     verdict="P(solvent at 95) is below your 90% target"
 *     numbers={[
 *       { label: "P(solvent at 95)", display: "87%" },
 *       { label: "Retire-ready age", display: "51", secondary: "was 49" },
 *       { label: "Top lever", display: "+5y working", secondary: "→ 93%" },
 *     ]}
 *   />
 */
export function HeroCard({ title, status, statusLabel, verdict, numbers, subline }: Props) {
  const s = STATUS_STYLE[status];
  return (
    <Card className={`relative overflow-hidden bg-gradient-to-r ${s.bar}`}>
      <div className="p-5">
        <div className="flex items-baseline justify-between gap-4 flex-wrap">
          <div className="flex items-center gap-3">
            <span
              className={`inline-block h-2.5 w-2.5 rounded-full ${s.dot}`}
              aria-hidden
            />
            <h2 className="text-lg font-semibold">{title}</h2>
            <span className={`text-xs font-mono font-semibold ${s.text}`}>
              {statusLabel ?? STATUS_LABEL[status]}
            </span>
          </div>
          {verdict && (
            <p className="text-sm text-muted-foreground">{verdict}</p>
          )}
        </div>

        <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-4">
          {numbers.map((n) => (
            <div key={n.label}>
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                {n.label}
              </div>
              <div className="mt-1 text-2xl font-mono font-semibold">
                {n.children ?? n.display}
              </div>
              {n.secondary && (
                <div className="text-xs text-muted-foreground">
                  {n.secondary}
                </div>
              )}
            </div>
          ))}
        </div>

        {subline && (
          <div className="mt-3 text-xs text-muted-foreground">{subline}</div>
        )}
      </div>
    </Card>
  );
}
