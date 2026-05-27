"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";

import { RetirementCard } from "@/components/portfolio/retirement-card";
import {
  StatCard,
  formatNis,
  formatPct,
  formatUsd,
} from "@/components/portfolio/stat-card";
import { api, type WealthDashboardDTO } from "@/lib/api";
import { cn } from "@/lib/utils";

interface WealthDashboardProps {
  userId: string;
}

/**
 * Top-of-/portfolio container that orchestrates all wealth-dashboard
 * sub-cards.
 *
 * Layout (matches the spec):
 *   ROW 1 — full-width retirement projection card (the headline).
 *   ROW 2 — 4-column grid: cash runway, NVDA concentration, savings rate,
 *           FX exposure.
 *   ROW 3 — 2-column grid: RSU income (next 12 months), estate exposure.
 *
 * Every block tolerates missing data: when the backend returns null
 * for a metric, the card renders "—" with the missing-data tooltip
 * surfaced via ``StatCard.missingReasons``.
 */
export function WealthDashboard({ userId }: WealthDashboardProps) {
  const [data, setData] = useState<WealthDashboardDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // We don't call setLoading(true) here because `loading` is initialised
    // to true in useState and never flips back to true (single fetch per
    // userId). The fetch lifecycle just transitions loading -> false at
    // the end; setting it true synchronously would trigger a cascading
    // render that the react-hooks/set-state-in-effect rule catches.
    api
      .wealthDashboard(userId)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading wealth dashboard…</p>;
  }
  if (error) {
    return <p className="text-sm text-error font-mono">{error}</p>;
  }
  if (!data) {
    return null;
  }

  return (
    <section className="flex flex-col gap-4" data-testid="wealth-dashboard">
      <RetirementCard
        retirement={data.retirement}
        assumptions={data.assumptions}
      />

      {/* Row 2: 4-column stat grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <CashRunwayCard block={data.cash_runway} />
        <ConcentrationCard block={data.concentration} />
        <SavingsRateCard block={data.savings_rate} />
        <FxExposureCard block={data.fx_exposure} />
      </div>

      {/* Row 3: 2-column rich-visual grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <RsuIncomeCard block={data.rsu_income} />
        <EstateExposureCard block={data.estate_exposure} />
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Row 2 — small stat cards
// ---------------------------------------------------------------------------

function CashRunwayCard({
  block,
}: {
  block: WealthDashboardDTO["cash_runway"];
}) {
  const months = block.months_of_runway;
  const tone =
    months == null
      ? "default"
      : months >= 12
        ? "success"
        : months >= 6
          ? "warning"
          : "error";
  const pctOfYear = months != null ? Math.min(months / 24, 1) : 0; // gauge: 0–24 mo
  const barColor =
    tone === "success" ? "bg-success" : tone === "warning" ? "bg-warning" : "bg-error";

  return (
    <StatCard
      eyebrow="Cash runway"
      value={
        months != null ? (
          <>
            {months.toFixed(1)}{" "}
            <span className="text-sm text-muted-foreground">months</span>
          </>
        ) : (
          "—"
        )
      }
      subline={
        block.defensive_total_nis != null
          ? `${formatNis(block.defensive_total_nis)} NIS cash + SGOV`
          : null
      }
      tone={tone === "default" ? "default" : tone}
      missingReasons={block.missing_reasons}
    >
      <div className="h-2 w-full rounded bg-muted/40 overflow-hidden">
        <div
          className={cn("h-full transition-all", barColor)}
          style={{ width: `${pctOfYear * 100}%` }}
        />
      </div>
      <div className="text-[10px] text-muted-foreground mt-1">
        green &gt;12 mo · yellow 6–12 · red &lt;6
      </div>
    </StatCard>
  );
}

function ConcentrationCard({
  block,
}: {
  block: WealthDashboardDTO["concentration"];
}) {
  const cur = block.current_pct;
  const tgt = block.target_pct;
  const delta = cur != null && tgt != null ? cur - tgt : null;
  const tone =
    delta == null
      ? "default"
      : delta <= 0
        ? "success"
        : delta <= 10
          ? "warning"
          : "error";
  // Bar fill = current %, target marker at target_pct (both relative to 100%).
  const fillPct = cur != null ? Math.min(cur, 100) : 0;
  const targetPos = tgt != null ? Math.min(tgt, 100) : null;

  const subline = (() => {
    if (cur == null || tgt == null) {
      return tgt == null
        ? "no plan target yet"
        : `target ${formatPct(tgt, 0)}`;
    }
    if (delta != null && delta > 0) {
      return `${formatPct(delta, 1)} above target ${formatPct(tgt, 0)}`;
    }
    return `${formatPct(Math.abs(delta ?? 0), 1)} below target ${formatPct(tgt, 0)}`;
  })();

  return (
    <StatCard
      eyebrow={`${block.symbol} concentration`}
      value={
        cur != null ? (
          <>
            {cur.toFixed(1)}
            <span className="text-sm text-muted-foreground">%</span>
          </>
        ) : (
          "—"
        )
      }
      subline={subline}
      tone={tone === "default" ? "default" : tone}
      missingReasons={block.missing_reasons}
    >
      <div className="relative h-2 w-full rounded bg-muted/40 overflow-hidden">
        <div
          className={cn(
            "h-full",
            tone === "success"
              ? "bg-success"
              : tone === "warning"
                ? "bg-warning"
                : tone === "error"
                  ? "bg-error"
                  : "bg-foreground/60",
          )}
          style={{ width: `${fillPct}%` }}
        />
        {targetPos != null && (
          <div
            className="absolute top-[-2px] h-3 w-0.5 bg-foreground"
            style={{ left: `${targetPos}%` }}
            aria-label={`target ${targetPos}%`}
          />
        )}
      </div>
    </StatCard>
  );
}

function SavingsRateCard({
  block,
}: {
  block: WealthDashboardDTO["savings_rate"];
}) {
  const rate = block.rate_pct;
  const tone =
    rate == null
      ? "default"
      : rate >= 30
        ? "success"
        : rate >= 10
          ? "warning"
          : "error";

  // Mini donut: filled arc proportional to savings rate. Pure SVG to avoid
  // a Recharts PieChart for a one-shot visual.
  const angle = rate != null ? (Math.max(0, Math.min(rate, 100)) / 100) * 360 : 0;
  const filledColor =
    tone === "success" ? "var(--color-success)" : tone === "warning" ? "var(--color-warning)" : "var(--color-error)";

  return (
    <StatCard
      eyebrow="Savings rate"
      value={
        rate != null ? (
          <>
            {rate.toFixed(0)}
            <span className="text-sm text-muted-foreground">%</span>
          </>
        ) : (
          "—"
        )
      }
      subline={
        block.monthly_income_nis != null && block.monthly_burn_nis != null
          ? `${formatNis(block.monthly_income_nis - block.monthly_burn_nis)} of ${formatNis(block.monthly_income_nis)} NIS`
          : null
      }
      tone={tone === "default" ? "default" : tone}
      missingReasons={block.missing_reasons}
    >
      <div className="flex items-center gap-3">
        <Donut angle={angle} color={filledColor} />
        <div className="text-[10px] text-muted-foreground leading-tight">
          <div>
            <span
              className="inline-block w-2 h-2 rounded-sm mr-1 align-middle"
              style={{ background: filledColor }}
            />
            saved
          </div>
          <div>
            <span className="inline-block w-2 h-2 rounded-sm mr-1 align-middle bg-muted" />
            spent
          </div>
        </div>
      </div>
    </StatCard>
  );
}

function FxExposureCard({
  block,
}: {
  block: WealthDashboardDTO["fx_exposure"];
}) {
  // Stacked horizontal bar by currency, normalised to 100%.
  const buckets = block.buckets;
  const palette: Record<string, string> = {
    USD: "var(--color-primary)",
    NIS: "var(--color-info)",
    EUR: "var(--color-warning)",
    OTHER: "var(--color-muted-foreground)",
  };

  return (
    <StatCard
      eyebrow="FX exposure"
      value={
        block.usd_pct != null ? (
          <>
            {block.usd_pct.toFixed(0)}
            <span className="text-sm text-muted-foreground">% USD</span>
          </>
        ) : (
          "—"
        )
      }
      subline={
        buckets.length > 0
          ? buckets.map((b) => `${b.currency} ${b.pct.toFixed(0)}%`).join(" · ")
          : null
      }
      missingReasons={block.missing_reasons}
    >
      <div className="flex h-3 w-full rounded overflow-hidden">
        {buckets.map((b) => (
          <div
            key={b.currency}
            style={{
              width: `${b.pct}%`,
              background: palette[b.currency] ?? "var(--color-muted-foreground)",
            }}
            title={`${b.currency}: ${b.pct.toFixed(1)}% (${formatNis(b.value_nis)} NIS)`}
          />
        ))}
      </div>
    </StatCard>
  );
}

// ---------------------------------------------------------------------------
// Row 3 — rich-visual cards
// ---------------------------------------------------------------------------

function RsuIncomeCard({
  block,
}: {
  block: WealthDashboardDTO["rsu_income"];
}) {
  const chartData = useMemo(
    () =>
      block.quarters.map((q) => ({
        period: q.period.split(" ")[0] ?? q.period,
        // Recharts can chart in millions to keep the y-axis readable.
        value_m: q.value_nis / 1_000_000,
        value_nis: q.value_nis,
        shares: q.shares,
      })),
    [block.quarters],
  );

  return (
    <StatCard
      eyebrow="RSU income · next 12 months"
      value={
        block.next_12_months_nis != null ? (
          <>
            {formatNis(block.next_12_months_nis)}{" "}
            <span className="text-sm text-muted-foreground">NIS</span>
          </>
        ) : (
          "—"
        )
      }
      subline={
        block.nvda_price_usd != null && block.fx_usd_nis != null
          ? `NVDA ${formatUsd(block.nvda_price_usd)} · USD/NIS ${block.fx_usd_nis.toFixed(3)}`
          : null
      }
      tone="success"
      missingReasons={block.missing_reasons}
      className="lg:col-span-1"
    >
      {chartData.length > 0 ? (
        <ResponsiveContainer width="100%" height={140}>
          <BarChart
            data={chartData}
            margin={{ top: 4, right: 8, bottom: 4, left: 0 }}
          >
            <XAxis dataKey="period" fontSize={10} />
            <YAxis
              fontSize={10}
              tickFormatter={(v) => `${v.toFixed(1)}M`}
              width={36}
            />
            <RechartsTooltip
              formatter={
                ((value: number) => [
                  `${formatNis(value * 1_000_000)} NIS`,
                  "value",
                ]) as unknown as never
              }
              labelFormatter={(label) => `${label}`}
              contentStyle={{
                background: "var(--color-popover)",
                border: "1px solid var(--color-border)",
                fontSize: 11,
              }}
            />
            <Bar dataKey="value_m" isAnimationActive={false}>
              {chartData.map((_, i) => (
                <Cell key={i} fill="var(--color-success)" />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="text-xs text-muted-foreground py-6 text-center">
          No vests in the next 12 months
        </div>
      )}
    </StatCard>
  );
}

function EstateExposureCard({
  block,
}: {
  block: WealthDashboardDTO["estate_exposure"];
}) {
  const usSitus = block.us_situs_usd;
  const liability = block.potential_liability_usd;
  const above = block.above_exemption_usd;
  const tone =
    liability == null
      ? "default"
      : liability >= 500_000
        ? "error"
        : liability >= 100_000
          ? "warning"
          : "success";

  return (
    <StatCard
      eyebrow="Estate exposure (US-situs)"
      value={
        usSitus != null ? (
          <>
            {formatUsd(usSitus)}{" "}
            <span className="text-sm text-muted-foreground">US-situs</span>
          </>
        ) : (
          "—"
        )
      }
      subline={
        liability != null
          ? `~${formatUsd(liability)} potential liability (40% on amount > $60k)`
          : null
      }
      tone={tone === "default" ? "default" : tone}
      missingReasons={block.missing_reasons}
    >
      {usSitus != null && (
        <div className="flex flex-col gap-1">
          {/* Exemption marker bar — fill is US-situs holdings, marker at exemption */}
          <div className="relative h-2 w-full rounded bg-muted/40 overflow-hidden">
            <div
              className={cn(
                "h-full",
                tone === "success"
                  ? "bg-success"
                  : tone === "warning"
                    ? "bg-warning"
                    : "bg-error",
              )}
              style={{
                width: `${Math.min((usSitus / Math.max(usSitus, block.nra_exemption_usd * 5)) * 100, 100)}%`,
              }}
            />
            <div
              className="absolute top-[-2px] h-3 w-0.5 bg-foreground"
              style={{
                left: `${Math.min((block.nra_exemption_usd / Math.max(usSitus, block.nra_exemption_usd * 5)) * 100, 100)}%`,
              }}
              aria-label={`exemption marker ${formatUsd(block.nra_exemption_usd)}`}
            />
          </div>
          <div className="text-[10px] text-muted-foreground">
            NRA exemption{" "}
            <span className="font-mono">
              {formatUsd(block.nra_exemption_usd)}
            </span>
            {above != null && above > 0 && (
              <>
                {" · "}
                <span className="text-warning">
                  {formatUsd(above)} above exemption
                </span>
              </>
            )}
          </div>
        </div>
      )}
    </StatCard>
  );
}

// ---------------------------------------------------------------------------
// Tiny inline visuals
// ---------------------------------------------------------------------------

/**
 * Inline SVG donut used by the savings-rate card. Stroke-dasharray approach
 * draws the filled arc proportional to ``angle`` (in degrees). 360 = full
 * circle.
 */
function Donut({ angle, color }: { angle: number; color: string }) {
  const size = 40;
  const stroke = 6;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const dash = (Math.min(Math.max(angle, 0), 360) / 360) * circumference;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="savings rate donut">
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="var(--color-muted)"
        strokeWidth={stroke}
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke={color}
        strokeWidth={stroke}
        strokeDasharray={`${dash} ${circumference - dash}`}
        strokeDashoffset={circumference / 4}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        strokeLinecap="butt"
      />
    </svg>
  );
}
