"use client";

/**
 * DualTrackAge — two honest retirement answers placed as markers on a shared
 * age axis: the "earliest safe" age (spend normally) and the "preservation"
 * age (keep every cent of principal). Plain SVG/CSS; the axis spans a small
 * pad around the two ages.
 */

export interface DualTrackAgeData {
  earliest_safe_age: number | null;
  preservation_age: number | null;
}

const EARLIEST_COLOR = "bg-success";
const PRESERVE_COLOR = "bg-info";

export function DualTrackAge({ data }: { data: DualTrackAgeData }) {
  const earliest =
    typeof data.earliest_safe_age === "number" ? data.earliest_safe_age : null;
  const preserve =
    typeof data.preservation_age === "number" ? data.preservation_age : null;

  if (earliest == null && preserve == null) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        Retirement-age estimates not available yet.
      </p>
    );
  }

  const ages = [earliest, preserve].filter(
    (v): v is number => typeof v === "number",
  );
  const lo = Math.min(...ages);
  const hi = Math.max(...ages);
  const pad = Math.max(2, (hi - lo) * 0.4 || 4);
  const axisMin = Math.floor(lo - pad);
  const axisMax = Math.ceil(hi + pad);
  const span = axisMax - axisMin || 1;
  const pctOf = (age: number) => ((age - axisMin) / span) * 100;

  const markers: Array<{
    age: number;
    label: string;
    sub: string;
    color: string;
  }> = [];
  if (earliest != null)
    markers.push({
      age: earliest,
      label: `Age ${Math.round(earliest)}`,
      sub: "Retire & spend normally",
      color: EARLIEST_COLOR,
    });
  if (preserve != null)
    markers.push({
      age: preserve,
      label: `Age ${Math.round(preserve)}`,
      sub: "Preserve all principal",
      color: PRESERVE_COLOR,
    });

  return (
    <div className="flex flex-col gap-4">
      <div className="relative h-px w-full bg-border">
        {/* the track */}
        <div className="absolute -top-[1px] h-[3px] w-full rounded-full bg-secondary/70" />
        {markers.map((m) => (
          <div
            key={m.label + m.sub}
            className="absolute -translate-x-1/2"
            style={{ left: `${Math.max(0, Math.min(100, pctOf(m.age)))}%` }}
          >
            <span
              className={`block h-3 w-3 -translate-y-1/2 rounded-full ${m.color} ring-2 ring-background`}
            />
          </div>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-4 text-xs">
        {markers.map((m) => (
          <div key={m.label + m.sub} className="flex items-start gap-2">
            <span
              className={`mt-0.5 inline-block h-3 w-3 shrink-0 rounded-full ${m.color}`}
            />
            <div>
              <div className="font-semibold text-foreground">{m.label}</div>
              <div className="text-muted-foreground">{m.sub}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="flex justify-between text-[10px] font-mono text-muted-foreground">
        <span>age {axisMin}</span>
        <span>age {axisMax}</span>
      </div>
    </div>
  );
}
