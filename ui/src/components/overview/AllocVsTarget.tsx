"use client";

/**
 * AllocVsTarget — per-class paired horizontal bars: where money sits NOW vs
 * the plan's TARGET mix. Plain divs. Detects value scale: if every number is
 * <= 1 the rows are fractions (0-1) and we render them *100; otherwise they're
 * already percentages.
 */

interface AllocRow {
  label: string;
  current_pct: number;
  target_pct: number;
}

export interface AllocVsTargetData {
  rows: AllocRow[];
}

function detectFractionScale(rows: AllocRow[]): boolean {
  // Fractions if every present value is within [0, 1].
  const vals: number[] = [];
  for (const r of rows) {
    if (typeof r.current_pct === "number") vals.push(r.current_pct);
    if (typeof r.target_pct === "number") vals.push(r.target_pct);
  }
  if (vals.length === 0) return false;
  return vals.every((v) => v >= 0 && v <= 1);
}

export function AllocVsTarget({ data }: { data: AllocVsTargetData }) {
  const rows = Array.isArray(data.rows) ? data.rows : [];
  if (rows.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        Allocation breakdown not available yet.
      </p>
    );
  }

  const fractionScale = detectFractionScale(rows);
  const toPct = (v: number) =>
    typeof v === "number" && Number.isFinite(v)
      ? fractionScale
        ? v * 100
        : v
      : 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-4 text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm bg-info/70" /> now
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-sm bg-success/70" />{" "}
          target
        </span>
      </div>
      <div className="flex flex-col gap-3">
        {rows.map((r) => {
          const cur = toPct(r.current_pct);
          const tgt = toPct(r.target_pct);
          return (
            <div key={r.label} className="flex flex-col gap-1">
              <div className="flex items-baseline justify-between text-xs">
                <span className="font-medium text-foreground">{r.label}</span>
                <span className="font-mono text-muted-foreground">
                  {cur.toFixed(0)}% → {tgt.toFixed(0)}%
                </span>
              </div>
              <div className="flex flex-col gap-1">
                <div className="h-2.5 w-full overflow-hidden rounded-full bg-secondary/50">
                  <div
                    className="h-full rounded-full bg-info/70"
                    style={{ width: `${Math.max(0, Math.min(100, cur))}%` }}
                  />
                </div>
                <div className="h-2.5 w-full overflow-hidden rounded-full bg-secondary/50">
                  <div
                    className="h-full rounded-full bg-success/70"
                    style={{ width: `${Math.max(0, Math.min(100, tgt))}%` }}
                  />
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
