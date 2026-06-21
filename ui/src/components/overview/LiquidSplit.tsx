"use client";

/**
 * LiquidSplit — a single horizontal segmented bar splitting net worth into
 * the liquid part ("spendable — counts toward retiring") and the illiquid
 * part ("home equity — real, but you can't live off it"). Plain divs; bar
 * geometry is the only thing computed from raw values, so it's scale-free.
 */

export interface LiquidSplitData {
  liquid_nis: number | null;
  illiquid_nis: number | null;
  total_nis: number | null;
}

function fmtNis(v: number | null | undefined): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `₪${(v / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `₪${(v / 1_000).toFixed(0)}K`;
  return `₪${v.toFixed(0)}`;
}

export function LiquidSplit({ data }: { data: LiquidSplitData }) {
  const liquid = typeof data.liquid_nis === "number" ? data.liquid_nis : 0;
  const illiquid =
    typeof data.illiquid_nis === "number" ? data.illiquid_nis : 0;
  const total =
    typeof data.total_nis === "number" && data.total_nis > 0
      ? data.total_nis
      : liquid + illiquid;

  const denom = total > 0 ? total : 1;
  const liquidPct = Math.max(0, Math.min(100, (liquid / denom) * 100));
  const illiquidPct = Math.max(0, Math.min(100, (illiquid / denom) * 100));

  const hasData = liquid > 0 || illiquid > 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex h-9 w-full overflow-hidden rounded-md border border-border bg-secondary/40">
        {hasData ? (
          <>
            <div
              className="flex items-center justify-center bg-success/70 text-[11px] font-medium text-success-foreground"
              style={{ width: `${liquidPct}%` }}
              title={`Spendable: ${fmtNis(liquid)}`}
            />
            <div
              className="flex items-center justify-center bg-muted-foreground/40 text-[11px] font-medium"
              style={{ width: `${illiquidPct}%` }}
              title={`Home equity: ${fmtNis(illiquid)}`}
            />
          </>
        ) : (
          <div className="flex w-full items-center justify-center text-xs text-muted-foreground">
            No balance-sheet data yet.
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div className="flex items-start gap-2">
          <span className="mt-0.5 inline-block h-3 w-3 shrink-0 rounded-sm bg-success/70" />
          <div>
            <div className="font-mono font-medium text-foreground">
              {fmtNis(liquid)}
            </div>
            <div className="text-muted-foreground">
              Spendable — counts toward retiring
            </div>
          </div>
        </div>
        <div className="flex items-start gap-2">
          <span className="mt-0.5 inline-block h-3 w-3 shrink-0 rounded-sm bg-muted-foreground/40" />
          <div>
            <div className="font-mono font-medium text-foreground">
              {fmtNis(illiquid)}
            </div>
            <div className="text-muted-foreground">
              Home equity — real, but doesn&apos;t count
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
