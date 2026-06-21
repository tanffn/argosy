"use client";

/**
 * NvdaWinddown — winding down the NVDA concentration bet.
 *
 * Two pieces:
 *  (1) a small glidepath line from current_pct → target_pct (with the cap
 *      drawn as a ceiling reference), and
 *  (2) a sell-now / wait split bar: of the shares the plan wants sold
 *      (sell_sh), how much is sellable at the low tax rate RIGHT NOW
 *      (eligible_now_sh) vs how much is worth waiting for.
 *
 * UNITS: current/target/cap_pct are FRACTIONS (0-1). Charts only need
 * relative geometry; where an axis/label shows a percent we normalize
 * (*100). Share counts are absolute.
 */

import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from "recharts";

export interface NvdaWinddownData {
  current_pct: number | null;
  target_pct: number | null;
  cap_pct: number | null;
  eligible_now_sh: number | null;
  sell_sh: number | null;
  target_sh: number | null;
  held_sh: number | null;
}

const CURRENT_COLOR = "#f97316"; // orange — today's over-weight
const TARGET_COLOR = "#10b981"; // emerald — plan target
const CAP_COLOR = "#f43f5e"; // rose — the hard cap ceiling

function asPct(v: number | null | undefined): number | null {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  return v * 100;
}

function fmtSh(v: number | null | undefined): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return `${Math.round(v).toLocaleString()} sh`;
}

export function NvdaWinddown({ data }: { data: NvdaWinddownData }) {
  const curPct = asPct(data.current_pct);
  const tgtPct = asPct(data.target_pct);
  const capPct = asPct(data.cap_pct);

  // Glidepath: a simple two-point line current → target (start = now,
  // end = "plan complete"). Scale-invariant; the y-axis is %.
  const glide =
    curPct != null && tgtPct != null
      ? [
          { step: "now", pct: curPct },
          { step: "plan", pct: tgtPct },
        ]
      : [];

  // Sell-now vs wait split. The "sellable now" slice is the eligible
  // shares, capped to the shares the plan wants sold; the remainder of
  // sell_sh is "worth waiting for". If sell_sh is unknown, fall back to
  // showing eligible as the whole sellable slice.
  const sell = typeof data.sell_sh === "number" ? data.sell_sh : null;
  const eligible =
    typeof data.eligible_now_sh === "number" ? data.eligible_now_sh : null;

  let sellNow = 0;
  let wait = 0;
  if (sell != null && sell > 0) {
    sellNow = eligible != null ? Math.min(eligible, sell) : 0;
    wait = Math.max(0, sell - sellNow);
  } else if (eligible != null) {
    sellNow = eligible;
    wait = 0;
  }
  const splitTotal = sellNow + wait;
  const sellNowPct = splitTotal > 0 ? (sellNow / splitTotal) * 100 : 0;
  const waitPct = splitTotal > 0 ? (wait / splitTotal) * 100 : 0;

  return (
    <div className="flex flex-col gap-5">
      {/* Glidepath */}
      <div>
        <div className="mb-1.5 text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
          Concentration glidepath
        </div>
        {glide.length === 2 ? (
          <ResponsiveContainer width="100%" height={150}>
            <LineChart
              data={glide}
              margin={{ top: 14, right: 16, bottom: 4, left: 0 }}
            >
              <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
              <XAxis dataKey="step" fontSize={11} />
              <YAxis
                fontSize={10}
                width={40}
                tickFormatter={(v) => `${Math.round(v)}%`}
                domain={[0, "dataMax"]}
              />
              {capPct != null && (
                <ReferenceLine
                  y={capPct}
                  stroke={CAP_COLOR}
                  strokeDasharray="4 4"
                  label={{
                    value: `cap ${capPct.toFixed(0)}%`,
                    position: "insideTopRight",
                    fill: CAP_COLOR,
                    fontSize: 10,
                  }}
                />
              )}
              <Line
                type="linear"
                dataKey="pct"
                stroke={CURRENT_COLOR}
                strokeWidth={2.5}
                dot={{ r: 4, fill: TARGET_COLOR }}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <p className="py-4 text-center text-sm text-muted-foreground">
            Glidepath not available yet.
          </p>
        )}
      </div>

      {/* Sell-now vs wait split bar */}
      <div>
        <div className="mb-1.5 text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
          Of the shares to trim
        </div>
        {splitTotal > 0 ? (
          <>
            <div className="flex h-8 w-full overflow-hidden rounded-md border border-border bg-secondary/40">
              <div
                className="flex items-center justify-center bg-success/70 text-[11px] font-medium text-success-foreground"
                style={{ width: `${sellNowPct}%` }}
                title={`Sellable now: ${fmtSh(sellNow)}`}
              />
              {wait > 0 && (
                <div
                  className="flex items-center justify-center bg-warning/60 text-[11px] font-medium"
                  style={{ width: `${waitPct}%` }}
                  title={`Worth waiting: ${fmtSh(wait)}`}
                />
              )}
            </div>
            <div className="mt-2 grid grid-cols-2 gap-3 text-xs">
              <div className="flex items-start gap-2">
                <span className="mt-0.5 inline-block h-3 w-3 shrink-0 rounded-sm bg-success/70" />
                <div>
                  <div className="font-mono font-medium text-foreground">
                    {fmtSh(sellNow)}
                  </div>
                  <div className="text-muted-foreground">
                    Sellable now (low tax rate)
                  </div>
                </div>
              </div>
              {wait > 0 && (
                <div className="flex items-start gap-2">
                  <span className="mt-0.5 inline-block h-3 w-3 shrink-0 rounded-sm bg-warning/60" />
                  <div>
                    <div className="font-mono font-medium text-foreground">
                      {fmtSh(wait)}
                    </div>
                    <div className="text-muted-foreground">
                      Worth waiting for
                    </div>
                  </div>
                </div>
              )}
            </div>
          </>
        ) : (
          <p className="py-4 text-center text-sm text-muted-foreground">
            No trim recommended right now.
          </p>
        )}
      </div>
    </div>
  );
}
