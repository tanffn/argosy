"use client";

/**
 * PhaseTimeline — life-event cashflow phases drawn as horizontal segments
 * along a shared age axis. Each phase shows its label and annual spend; the
 * segment's left offset + width come from its age span (scale-invariant).
 *
 * The backend may key the span as start_age/end_age (task contract) or
 * start/end (spec §4.6); we read either. An open-ended final phase (no end)
 * runs to the max age seen.
 */

interface PhaseRaw {
  label: string;
  start_age?: number | null;
  end_age?: number | null;
  start?: number | null;
  end?: number | null;
  annual_nis: number | null;
}

export interface PhaseTimelineData {
  phases: PhaseRaw[];
}

interface Phase {
  label: string;
  start: number;
  end: number;
  annual_nis: number | null;
}

const SEGMENT_COLORS = [
  "bg-info/60",
  "bg-success/60",
  "bg-warning/60",
  "bg-primary/50",
  "bg-muted-foreground/40",
];

function fmtNis(v: number | null | undefined): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `₪${(v / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `₪${(v / 1_000).toFixed(0)}K`;
  return `₪${v.toFixed(0)}`;
}

export function PhaseTimeline({ data }: { data: PhaseTimelineData }) {
  const raw = Array.isArray(data.phases) ? data.phases : [];

  const starts = raw
    .map((p) => (typeof p.start_age === "number" ? p.start_age : p.start))
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v));
  const ends = raw
    .map((p) => (typeof p.end_age === "number" ? p.end_age : p.end))
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v));

  if (starts.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        Life-phase timeline not available yet.
      </p>
    );
  }

  const axisMin = Math.min(...starts);
  const axisMax = Math.max(...starts, ...ends, axisMin + 1);

  const phases: Phase[] = raw.map((p) => {
    const start =
      typeof p.start_age === "number"
        ? p.start_age
        : typeof p.start === "number"
          ? p.start
          : axisMin;
    const endRaw =
      typeof p.end_age === "number"
        ? p.end_age
        : typeof p.end === "number"
          ? p.end
          : null;
    const end = endRaw == null ? axisMax : endRaw;
    return { label: p.label, start, end, annual_nis: p.annual_nis ?? null };
  });

  const span = axisMax - axisMin || 1;
  const pctOf = (age: number) => ((age - axisMin) / span) * 100;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2">
        {phases.map((p, i) => {
          const left = Math.max(0, Math.min(100, pctOf(p.start)));
          const width = Math.max(
            2,
            Math.min(100 - left, pctOf(p.end) - pctOf(p.start)),
          );
          return (
            <div key={`${p.label}-${i}`} className="flex flex-col gap-0.5">
              <div className="flex items-baseline justify-between text-xs">
                <span className="font-medium text-foreground">{p.label}</span>
                <span className="font-mono text-muted-foreground">
                  age {Math.round(p.start)}–{Math.round(p.end)} ·{" "}
                  {fmtNis(p.annual_nis)}/yr
                </span>
              </div>
              <div className="relative h-4 w-full rounded-full bg-secondary/40">
                <div
                  className={`absolute top-0 h-4 rounded-full ${
                    SEGMENT_COLORS[i % SEGMENT_COLORS.length]
                  }`}
                  style={{ left: `${left}%`, width: `${width}%` }}
                  title={`age ${Math.round(p.start)}–${Math.round(p.end)}`}
                />
              </div>
            </div>
          );
        })}
      </div>
      {/* Age axis ticks */}
      <div className="flex justify-between text-[10px] font-mono text-muted-foreground">
        <span>age {Math.round(axisMin)}</span>
        <span>age {Math.round(axisMax)}</span>
      </div>
    </div>
  );
}
