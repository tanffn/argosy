"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type WindfallClassifiedSource,
  type WindfallDetectResponse,
} from "@/lib/api";

/**
 * Home-page banner for the auto-detected windfall flow.
 *
 * Calls GET /api/retirement/windfall/detect on mount. The endpoint diffs
 * the two most-recent monthly TSVs in $ARGOSY_EXPENSE_SAMPLES_ROOT and
 * fires only when the cash delta crosses $25K USD or ₪75K NIS. When no
 * event is detected (or fewer than 2 TSVs on disk), the banner renders
 * nothing — keeping a clean home page in the common case.
 *
 * Tone follows classification: WARN (amber) when classified_source is
 * "unclear" because the user still needs to weigh in; INFO (blue) once
 * the detector matched equity sales to the cash delta.
 */
export function WindfallBanner() {
  const [data, setData] = useState<WindfallDetectResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .windfallDetect()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => {
        // Swallow — endpoint can legitimately fail (no TSV root mounted,
        // backend down). Banner stays hidden; user notices nothing.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!data?.event) return null;
  const e = data.event;

  const isUnclear = e.classified_source === "unclear";
  const borderClass = isUnclear ? "border-l-warning/80" : "border-l-info/80";
  const dotClass = isUnclear ? "text-warning" : "text-info";
  // Glyph mirrors the tone — ⚠ only when the user actually needs to
  // weigh in. A confidently-classified RSU/stock sale gets a $-mark
  // so a green-path event isn't visually flagged as a warning.
  const glyph = isUnclear ? "⚠" : "$";

  return (
    <section
      className={`rounded-lg border border-border ${borderClass} border-l-2 bg-card px-4 py-3 flex items-center justify-between gap-3 flex-wrap`}
      data-slot="windfall-banner"
    >
      <div className="flex flex-col gap-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span aria-hidden className={`font-mono text-sm ${dotClass}`}>
            {glyph}
          </span>
          <span className="font-mono text-sm font-semibold">
            {formatUsd(e.cash_delta_total_usd_equiv)} windfall detected in{" "}
            {humanTsvLabel(e.source_tsv)}
          </span>
          <StatusPill tone={isUnclear ? "warning" : "accent"} mono>
            {classificationLabel(e.classified_source)}
          </StatusPill>
          {e.requires_user_classification ? (
            <StatusPill tone="warning" mono>
              NEEDS REVIEW
            </StatusPill>
          ) : null}
        </div>
        <div className="font-mono text-[11px] text-muted-foreground tabular-nums">
          {salesSummary(e.matching_sales)}
        </div>
        <div className="font-mono text-[11px] text-muted-foreground">
          compared {humanTsvLabel(e.previous_tsv ?? "")} →{" "}
          {humanTsvLabel(e.source_tsv)}
        </div>
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        <Link
          href="/proposals#allocation"
          className="font-mono text-xs text-info hover:underline"
        >
          See allocation plan -&gt;
        </Link>
      </div>
    </section>
  );
}

function formatUsd(value: number): string {
  if (!Number.isFinite(value)) return "$—";
  return `$${Math.round(value).toLocaleString()}`;
}

function classificationLabel(source: WindfallClassifiedSource): string {
  switch (source) {
    case "rsu_sale":
      return "RSU SALE";
    case "stock_sale":
      return "STOCK SALE";
    default:
      return "UNCLEAR";
  }
}

// "Family Finances Status - 26 May.tsv" → "May 2026". Falls back to the
// raw filename when the pattern doesn't match — the detector pins the
// "YY MMM" convention so this is the common case.
function humanTsvLabel(filename: string): string {
  if (!filename) return "—";
  const m = filename.match(/(\d{2})\s+([A-Za-z]{3})/);
  if (!m) return filename;
  const [, yy, mon] = m;
  const year = 2000 + Number(yy);
  return `${mon} ${year}`;
}

function salesSummary(
  sales: { symbol: string; shares_sold: number }[],
): string {
  if (sales.length === 0) return "no matching equity sales in the same month";
  const parts = sales.map(
    (s) => `${s.symbol} -${Math.abs(s.shares_sold).toLocaleString()}`,
  );
  return `matching sales: ${parts.join(" + ")}`;
}
