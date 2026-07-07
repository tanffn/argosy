"use client";

/**
 * WealthTrajectoryCard — what an FM would actually show for "how is the
 * book doing", at QUARTERLY resolution: exactly 1 year of ACTUAL net
 * worth (portfolio_snapshots history via GET
 * /api/portfolio/net-worth-history — POSITIONS-SUM basis, one basis for
 * every row provenance) plus exactly 1 year PROJECTED from the
 * wealth-dashboard's canonical scenario trajectory, rendered as a
 * dashed median line inside a shaded bear→typical band labelled
 * "projected". The x-axis always spans the full window (1y back → 1y
 * forward) even when ingestion history is younger — missing quarters
 * render as visible gap, with an explicit "history begins <Mon YYYY>"
 * note.
 *
 * Currency: a USD / ₪ toggle (USD default, persisted in localStorage).
 * The ₪ series is served per-point at each snapshot's OWN fx (real
 * NIS-perspective wealth — what matters for FI-in-Israel), never a
 * single-rate rescale.
 *
 * Universe: the PORTFOLIO BOOK (labelled so). The projection applies
 * the trajectory's growth RATES anchored at the latest actual — its
 * absolute levels are total net worth (pensions + real estate) and
 * would jump ~$0.7M at today if plotted directly. Rates are unitless,
 * so the anchored projection is currency-consistent by construction.
 *
 * Tooltip on actual points decomposes the move vs the previous
 * quarterly actual: total · NVDA · cash · FX (translation) · other —
 * every number derived from stored per-snapshot fields, nothing
 * smoothed or clamped.
 *
 * The header pill answers "are we beating the trendline": a linear fit
 * through the past year's actuals, evaluated at the latest snapshot.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type NetWorthHistoryPointDTO,
  type NetWorthHistoryResponse,
  type WealthDashboardDTO,
} from "@/lib/api";

const PAST_QUARTERS = 4; // exactly 1 year back
const PROJECTION_QUARTERS = 4; // exactly 1 year forward

const ACTUAL_COLOR = "#10b981"; // emerald — realized
const PROJECTED_COLOR = "#6366f1"; // indigo — model output

const QUARTER_MS = (365.25 / 4) * 24 * 3600 * 1000;

export type WealthCurrency = "USD" | "NIS";
const CURRENCY_STORAGE_KEY = "argosy:wealth-trajectory-currency";

export interface DeltaBreakdown {
  total: number;
  nvda: number | null;
  cash: number | null;
  /** Cross-currency translation term (explicit FX portion). */
  fx: number | null;
  /** total − known components (residual: repricing of the rest + flows). */
  other: number;
}

interface Row {
  ts: number; // epoch ms — numeric x-axis handles irregular spacing
  actual?: number;
  projMid?: number;
  projBand?: [number, number];
  /** The raw history point behind an actual (for the delta tooltip). */
  point?: NetWorthHistoryPointDTO;
  /** Move vs the previous quarterly actual, decomposed. */
  delta?: DeltaBreakdown;
}

export function formatMoneyCompact(v: number, currency: WealthCurrency): string {
  if (!Number.isFinite(v)) return "—";
  const sym = currency === "USD" ? "$" : "₪";
  if (Math.abs(v) >= 1_000_000) return `${sym}${(v / 1_000_000).toFixed(2)}M`;
  return `${sym}${Math.round(v / 1_000).toLocaleString()}K`;
}

/** Kept for back-compat with existing imports/tests. */
export function formatUsdCompact(v: number): string {
  return formatMoneyCompact(v, "USD");
}

function signed(v: number, currency: WealthCurrency): string {
  return `${v >= 0 ? "+" : "−"}${formatMoneyCompact(Math.abs(v), currency)}`;
}

function monthLabel(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleDateString([], { month: "short", year: "2-digit" });
}

function pointValue(
  p: NetWorthHistoryPointDTO,
  currency: WealthCurrency,
): number | null {
  return currency === "USD" ? p.total_usd : (p.total_nis ?? null);
}

/**
 * Decompose the move between two consecutive snapshots in the active
 * currency. Components are computed at the PREVIOUS point's fx (local
 * moves), the FX term carries the translation of the non-native part of
 * the book, and ``other`` is the exact residual — the parts always
 * re-sum to the total (nothing absorbed silently).
 */
export function deltaBreakdown(
  prev: NetWorthHistoryPointDTO,
  cur: NetWorthHistoryPointDTO,
  currency: WealthCurrency,
): DeltaBreakdown | null {
  const pv = pointValue(prev, currency);
  const cv = pointValue(cur, currency);
  if (pv === null || cv === null) return null;
  const total = cv - pv;

  const pFx = prev.fx_usd_nis ?? null;
  const cFx = cur.fx_usd_nis ?? null;
  const bothNvda = prev.nvda_usd != null && cur.nvda_usd != null;
  const bothCash = prev.cash_usd != null && cur.cash_usd != null;

  let nvda: number | null = null;
  let cash: number | null = null;
  let fx: number | null = null;

  if (currency === "USD") {
    nvda = bothNvda ? cur.nvda_usd! - prev.nvda_usd! : null;
    cash = bothCash ? cur.cash_usd! - prev.cash_usd! : null;
    // NIS-denominated assets change USD value when fx moves:
    // Δ = N/fx_cur − N/fx_prev with N = nis_usd_prev × fx_prev.
    if (prev.nis_denominated_usd != null && pFx && cFx) {
      fx = prev.nis_denominated_usd * (pFx / cFx - 1);
    }
  } else {
    // Local (constant-fx) component moves, valued at the previous fx.
    nvda = bothNvda && pFx ? (cur.nvda_usd! - prev.nvda_usd!) * pFx : null;
    cash = bothCash && pFx ? (cur.cash_usd! - prev.cash_usd!) * pFx : null;
    // USD-denominated assets change NIS value when fx moves.
    if (
      prev.total_usd != null &&
      prev.nis_denominated_usd != null &&
      pFx &&
      cFx
    ) {
      fx = (cFx - pFx) * (prev.total_usd - prev.nis_denominated_usd);
    }
  }

  const other = total - (nvda ?? 0) - (cash ?? 0) - (fx ?? 0);
  return { total, nvda, cash, fx, other };
}

/**
 * Quarterly actuals in the active currency: the LAST snapshot per
 * calendar quarter within the past year, each annotated with the
 * decomposed delta vs the previous bucketed point.
 */
export function bucketQuarterly(
  history: NetWorthHistoryResponse | null,
  now: number,
  currency: WealthCurrency = "USD",
): Row[] {
  const cutoff = now - PAST_QUARTERS * QUARTER_MS;
  const byQuarter = new Map<string, Row>();
  for (const p of history?.points ?? []) {
    const value = pointValue(p, currency);
    if (value === null) continue;
    const ts = new Date(p.date).getTime();
    if (Number.isNaN(ts) || ts < cutoff || ts > now) continue;
    const d = new Date(ts);
    const key = `${d.getFullYear()}-q${Math.floor(d.getMonth() / 3)}`;
    const prev = byQuarter.get(key);
    if (!prev || ts > prev.ts) {
      byQuarter.set(key, { ts, actual: value, point: p });
    }
  }
  const rows = [...byQuarter.values()].sort((a, b) => a.ts - b.ts);
  for (let i = 1; i < rows.length; i++) {
    const prevPt = rows[i - 1].point;
    const curPt = rows[i].point;
    if (prevPt && curPt) {
      rows[i].delta = deltaBreakdown(prevPt, curPt, currency) ?? undefined;
    }
  }
  return rows;
}

/**
 * Quarterly projection ANCHORED at the latest actual: the canonical
 * trajectory's year-0 → year-1 GROWTH RATES (geometric quarterly)
 * applied forward from ``anchor`` in the ACTIVE currency — rates are
 * unitless, so the anchored projection is currency-consistent with no
 * conversion. Fallback with no anchor: absolute levels (the trajectory
 * is NIS-native — served raw in ₪ view, ÷fx in USD view; omitted when
 * fx is missing in USD view — never mixed units).
 */
export function buildProjection(
  dash: WealthDashboardDTO | null,
  now: number,
  anchor: number | null,
  currency: WealthCurrency = "USD",
): Row[] {
  const traj = dash?.retirement.trajectory ?? [];
  const y0 = traj.find((t) => t.year === 0);
  const y1 = traj.find((t) => t.year === 1);
  if (!y0 || !y1) return [];
  const rows: Row[] = [];

  if (anchor !== null && anchor > 0 && y0.typical > 0 && y0.bear > 0) {
    // Geometric quarterly growth ratio between year-0 and year-1 levels.
    const ratio = (a: number, b: number, q: number): number =>
      b > 0 ? Math.pow(b / a, q / 4) : 1;
    for (let q = 0; q <= PROJECTION_QUARTERS; q++) {
      const typical = anchor * ratio(y0.typical, y1.typical, q);
      const bear = anchor * ratio(y0.bear, y1.bear, q);
      rows.push({
        ts: now + q * QUARTER_MS,
        projMid: typical,
        projBand: [Math.min(bear, typical), Math.max(bear, typical)],
      });
    }
    return rows;
  }

  // No anchor — absolute levels. The trajectory is NIS-native.
  let divisor = 1;
  if (currency === "USD") {
    const fx = dash?.assumptions.fx_usd_nis ?? null;
    if (fx === null || fx <= 0) return [];
    divisor = fx;
  }
  const interp = (a: number, b: number, q: number): number =>
    a > 0 && b > 0 ? a * Math.pow(b / a, q / 4) : a + (b - a) * (q / 4);
  for (let q = 0; q <= PROJECTION_QUARTERS; q++) {
    const bear = interp(y0.bear, y1.bear, q) / divisor;
    const typical = interp(y0.typical, y1.typical, q) / divisor;
    rows.push({
      ts: now + q * QUARTER_MS,
      projMid: typical,
      projBand: [Math.min(bear, typical), Math.max(bear, typical)],
    });
  }
  return rows;
}

/**
 * "Are we beating the trendline" — least-squares linear fit through the
 * past year's quarterly actuals, evaluated at the latest actual point.
 * Returns the residual (latest − fit); null when fewer than 3 points
 * (a 2-point fit is exact, so the residual is vacuously 0).
 */
export function trendDelta(actuals: Row[]): number | null {
  const pts = actuals.filter((r) => r.actual !== undefined);
  if (pts.length < 3) return null;
  const n = pts.length;
  // Quarter-scaled, origin-shifted x for numeric stability.
  const x0 = pts[0].ts;
  let sx = 0;
  let sy = 0;
  let sxx = 0;
  let sxy = 0;
  for (const p of pts) {
    const x = (p.ts - x0) / QUARTER_MS;
    const y = p.actual!;
    sx += x;
    sy += y;
    sxx += x * x;
    sxy += x * y;
  }
  const denom = n * sxx - sx * sx;
  if (denom === 0) return null;
  const b = (n * sxy - sx * sy) / denom;
  const a = (sy - b * sx) / n;
  const last = pts[n - 1];
  const fit = a + b * ((last.ts - x0) / QUARTER_MS);
  return last.actual! - fit;
}

function TrajectoryTooltip(props: {
  active?: boolean;
  label?: number;
  currency: WealthCurrency;
  payload?: {
    name?: string;
    value?: number | number[];
    color?: string;
    payload?: Row;
  }[];
}) {
  const { active, payload, label, currency } = props;
  if (!active || !payload || payload.length === 0) return null;
  const row = payload[0]?.payload;
  const delta = row?.delta;
  return (
    <div className="rounded-md border border-border/60 bg-popover text-popover-foreground text-xs shadow p-2 max-w-xs">
      <p className="font-semibold mb-1">
        {typeof label === "number" ? monthLabel(label) : ""}
      </p>
      <ul className="flex flex-col gap-0.5">
        {payload.map((entry, i) => (
          <li key={i} className="flex items-baseline gap-2">
            <span
              className="w-2 h-2 rounded-sm inline-block"
              style={{ backgroundColor: entry.color }}
            />
            <span className="flex-1">{entry.name}</span>
            <span className="font-mono">
              {Array.isArray(entry.value)
                ? `${formatMoneyCompact(entry.value[0], currency)} – ${formatMoneyCompact(entry.value[1], currency)}`
                : typeof entry.value === "number"
                  ? formatMoneyCompact(entry.value, currency)
                  : "—"}
            </span>
          </li>
        ))}
      </ul>
      {delta ? (
        <div
          className="mt-1.5 pt-1.5 border-t border-border/60 font-mono tabular-nums text-muted-foreground"
          data-testid="delta-breakdown"
        >
          <p>Δ vs prev actual: {signed(delta.total, currency)}</p>
          <p>
            {delta.nvda !== null ? `NVDA ${signed(delta.nvda, currency)} · ` : ""}
            {delta.cash !== null ? `cash ${signed(delta.cash, currency)} · ` : ""}
            {delta.fx !== null ? `FX ${signed(delta.fx, currency)} · ` : ""}
            other {signed(delta.other, currency)}
          </p>
        </div>
      ) : null}
    </div>
  );
}

export function WealthTrajectoryCard({ userId }: { userId: string }) {
  const [history, setHistory] = useState<NetWorthHistoryResponse | null>(null);
  const [dash, setDash] = useState<WealthDashboardDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [currency, setCurrency] = useState<WealthCurrency>("USD");
  // Projection anchor "now" — stamped from the fetch effect so render
  // stays pure (react-hooks/purity forbids Date.now() during render).
  const [nowTs, setNowTs] = useState<number | null>(null);

  // Restore the persisted currency choice (client-only). Deferred to
  // the next frame so the effect body never calls setState
  // synchronously (react-hooks/set-state-in-effect) — same pattern as
  // LiveClock.
  useEffect(() => {
    let cancelled = false;
    const raf = window.requestAnimationFrame(() => {
      if (cancelled) return;
      try {
        const saved = window.localStorage.getItem(CURRENCY_STORAGE_KEY);
        if (saved === "USD" || saved === "NIS") setCurrency(saved);
      } catch {
        /* storage unavailable — keep the USD default */
      }
    });
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(raf);
    };
  }, []);

  const chooseCurrency = useCallback((c: WealthCurrency) => {
    setCurrency(c);
    try {
      window.localStorage.setItem(CURRENCY_STORAGE_KEY, c);
    } catch {
      /* storage unavailable — the choice still applies this session */
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      api.netWorthHistory(userId, 12),
      api.wealthDashboard(userId),
    ]).then(([h, d]) => {
      if (cancelled) return;
      if (h.status === "fulfilled") setHistory(h.value);
      if (d.status === "fulfilled") setDash(d.value);
      setNowTs(Date.now());
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const actuals = useMemo(
    () => bucketQuarterly(history, nowTs ?? 0, currency),
    [history, nowTs, currency],
  );
  // Anchor the projection at the latest ACTUAL value in the active
  // currency — one universe, and the projected line starts where the
  // actual line ends.
  const anchor = useMemo(() => {
    const last = actuals.length > 0 ? actuals[actuals.length - 1] : null;
    return last?.actual ?? null;
  }, [actuals]);
  const projection = useMemo(
    () => buildProjection(dash, nowTs ?? 0, anchor, currency),
    [dash, nowTs, anchor, currency],
  );
  const rows = useMemo(
    () => [...actuals, ...projection].sort((a, b) => a.ts - b.ts),
    [actuals, projection],
  );
  const delta = useMemo(() => trendDelta(actuals), [actuals]);

  const hasActual = actuals.length > 0;
  const hasProjection = projection.length > 0;
  const latestActual = hasActual ? actuals[actuals.length - 1] : null;
  // The axis always spans the full window: 1y back from today.
  const windowStart = nowTs !== null ? nowTs - PAST_QUARTERS * QUARTER_MS : null;

  // Ingestion only started recently — say so instead of implying a
  // silently missing year of history (~2 months of slack on the window).
  const historyBeginsNote = useMemo(() => {
    if (actuals.length === 0 || nowTs === null) return null;
    const first = actuals[0];
    if (first.ts > nowTs - (PAST_QUARTERS - 0.7) * QUARTER_MS) {
      const label = new Date(first.ts).toLocaleDateString([], {
        month: "short",
        year: "numeric",
      });
      return `history begins ${label}`;
    }
    return null;
  }, [actuals, nowTs]);

  return (
    <div
      className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-1"
      data-slot="wealth-trajectory"
    >
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
          Wealth trajectory — portfolio (book)
        </span>
        <div className="flex items-center gap-2">
          {latestActual?.actual !== undefined ? (
            <span
              className="font-mono text-sm font-semibold tabular-nums"
              data-testid="wealth-latest"
            >
              {formatMoneyCompact(latestActual.actual, currency)}
            </span>
          ) : null}
          {delta !== null ? (
            Math.abs(delta) < 500 ? (
              <StatusPill tone="success" mono data-testid="wealth-trend">
                ON TREND
              </StatusPill>
            ) : (
              <StatusPill
                tone={delta >= 0 ? "success" : "warning"}
                mono
                data-testid="wealth-trend"
                title="latest snapshot vs a linear fit through the past year's quarterly actuals"
              >
                {delta >= 0
                  ? `AHEAD OF TREND +${formatMoneyCompact(delta, currency)}`
                  : `BEHIND TREND −${formatMoneyCompact(Math.abs(delta), currency)}`}
              </StatusPill>
            )
          ) : null}
          {/* USD / ₪ toggle — persisted. */}
          <span
            className="inline-flex rounded-md border border-border overflow-hidden"
            role="group"
            aria-label="Currency"
          >
            {(["USD", "NIS"] as const).map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => chooseCurrency(c)}
                aria-pressed={currency === c}
                data-testid={`currency-${c}`}
                className={`px-2 py-0.5 font-mono text-[11px] transition-colors ${
                  currency === c
                    ? "bg-secondary text-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {c === "USD" ? "USD" : "₪"}
              </button>
            ))}
          </span>
        </div>
      </div>
      {loading ? (
        <div
          className="h-[180px] flex items-center justify-center text-xs text-muted-foreground font-mono"
          data-testid="wealth-loading"
        >
          loading…
        </div>
      ) : rows.length === 0 ? (
        <div
          className="h-[180px] flex items-center justify-center text-xs text-muted-foreground font-mono"
          data-testid="wealth-empty"
        >
          No snapshot history yet.
        </div>
      ) : (
        <>
          <ResponsiveContainer width="100%" height={180}>
            <ComposedChart
              data={rows}
              margin={{ top: 8, right: 8, bottom: 0, left: 4 }}
            >
              <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
              <XAxis
                dataKey="ts"
                type="number"
                scale="time"
                // Full 1y-back window even when history is younger —
                // the pre-ingestion quarters render as a visible gap.
                domain={[windowStart ?? "dataMin", "dataMax"]}
                allowDataOverflow
                tickFormatter={monthLabel}
                fontSize={10}
                minTickGap={30}
              />
              <YAxis
                fontSize={10}
                width={52}
                tickFormatter={(v: number) => formatMoneyCompact(v, currency)}
                domain={["auto", "auto"]}
              />
              <Tooltip
                content={<TrajectoryTooltip currency={currency} />}
              />
              {hasProjection ? (
                <Area
                  dataKey="projBand"
                  name="projected (bear–typical)"
                  stroke="none"
                  fill={PROJECTED_COLOR}
                  fillOpacity={0.15}
                  connectNulls
                  isAnimationActive={false}
                />
              ) : null}
              <Line
                dataKey="actual"
                name="actual"
                stroke={ACTUAL_COLOR}
                strokeWidth={2}
                dot={{ r: 3 }}
                connectNulls
                isAnimationActive={false}
              />
              {hasProjection ? (
                <Line
                  dataKey="projMid"
                  name="projected"
                  stroke={PROJECTED_COLOR}
                  strokeWidth={2}
                  strokeDasharray="5 4"
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                />
              ) : null}
            </ComposedChart>
          </ResponsiveContainer>
          <div className="text-[11px] text-muted-foreground tabular-nums">
            {hasActual ? "solid: past year actual (quarterly)" : null}
            {hasActual && hasProjection ? " · " : null}
            {hasProjection
              ? "dashed/shaded: 1y projected (canonical growth rates, anchored at the latest actual)"
              : null}
            {historyBeginsNote ? ` · ${historyBeginsNote}` : null}
          </div>
        </>
      )}
    </div>
  );
}
