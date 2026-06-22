"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type WindfallClassifiedSource,
  type WindfallDetectResponse,
  type WindfallEventDTO,
} from "@/lib/api";

/**
 * Home-page banner for the auto-detected cash-position change.
 *
 * Calls GET /api/retirement/windfall/detect on mount. The endpoint diffs
 * the two most-recent monthly TSVs in $ARGOSY_EXPENSE_SAMPLES_ROOT and
 * fires only when the cash delta crosses $25K USD or ₪75K NIS. When no
 * event is detected (or fewer than 2 TSVs on disk), the banner renders
 * nothing — keeping a clean home page in the common case.
 *
 * This is NOT a "windfall" / gift banner: the cash delta is a month-over-
 * month change in the cash position that needs allocating. Its likely
 * SOURCE is named (a matched equity sale, or — when nothing matches — an
 * unexplained move that's probably a reallocation). Tone follows
 * confidence: WARN (amber) when the source is unexplained and the user
 * needs to weigh in; INFO (blue) once a sale explains the delta.
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

  const frame = describeCashChange(e);
  const borderClass = frame.tone === "warn" ? "border-l-warning/80" : "border-l-info/80";
  const dotClass = frame.tone === "warn" ? "text-warning" : "text-info";
  // Glyph mirrors the tone — ⚠ only when the source is unexplained and
  // the user actually needs to weigh in. A sale-explained delta gets a
  // $-mark so an explained event isn't visually flagged as a warning.
  const glyph = frame.tone === "warn" ? "⚠" : "$";

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
            {frame.headline} in {humanTsvLabel(e.source_tsv)}
          </span>
          <StatusPill tone={frame.tone === "warn" ? "warning" : "accent"} mono>
            {frame.pill}
          </StatusPill>
          {e.requires_user_classification ? (
            <StatusPill tone="warning" mono>
              NEEDS REVIEW
            </StatusPill>
          ) : null}
        </div>
        <div className="font-mono text-[11px] text-muted-foreground tabular-nums">
          {frame.breakdownLine}
        </div>
        <div className="font-mono text-[11px] text-muted-foreground">
          compared {humanTsvLabel(e.previous_tsv ?? "")} →{" "}
          {humanTsvLabel(e.source_tsv)}
        </div>
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        <Link
          href="/inbox#allocation"
          className="font-mono text-xs text-info hover:underline"
        >
          Allocate this cash -&gt;
        </Link>
      </div>
    </section>
  );
}

// ---------- cash-change framing ----------------------------------------

interface CashChangeFrame {
  tone: "warn" | "info";
  /** Plain-language headline — never implies free money. */
  headline: string;
  pill: string;
  /** One-line source/breakdown hint. */
  breakdownLine: string;
}

/**
 * Turn a raw cash delta into honest, source-driven copy.
 *
 * Discriminator order:
 *   1. matching_sales non-empty → name the sale(s); call out the
 *      unexplained residual when the sales don't cover the full delta.
 *   2. classified_source salary/bonus → "new cash from <source>".
 *      (Not in the current TS union, but handled defensively in case the
 *      backend widens it — see api.ts WindfallClassifiedSource.)
 *   3. otherwise → unexplained change, likely a reallocation/sale.
 */
function describeCashChange(e: WindfallEventDTO): CashChangeFrame {
  const total = e.cash_delta_total_usd_equiv;
  const totalStr = formatUsd(total);

  // Transaction-based reconciliation supersedes the matching_sales
  // heuristic: when the inflow is linked 1:1 to RSU sales with a negligible
  // residual, the source IS RSU sales — never "unexplained". Mirrors
  // isFullyReconciled() in WindfallCard.
  const reconciledLines = e.reconciled_source_lines ?? [];
  const reconciledUnexplained = Math.abs(e.reconciled_unexplained_usd ?? 0);
  const reconciledBand = Math.max(1, 0.05 * Math.abs(total));
  if (reconciledLines.length > 0 && reconciledUnexplained < reconciledBand) {
    return {
      tone: "info",
      headline: `Cash position changed by ${totalStr}`,
      pill: "RSU SALE",
      breakdownLine:
        "sourced from NVDA RSU sales — reconciled to your Leumi USD transfers, $0 unexplained; to allocate",
    };
  }

  if (e.matching_sales.length > 0) {
    const matched = e.matching_sales.reduce((acc, s) => acc + s.value_usd, 0);
    const residual = total - matched;
    const saleNames = e.matching_sales
      .map((s) => `${s.symbol} (~${formatUsd(s.value_usd)})`)
      .join(" + ");
    // A material residual means a chunk of the delta isn't explained by
    // the matched sale(s) — flag it so the user knows it's not "just" the
    // sale. Threshold mirrors the detector's 5% match band.
    const materialResidual = Math.abs(residual) > 0.05 * Math.abs(total);
    return {
      tone: materialResidual ? "warn" : "info",
      headline: `Cash position changed by ${totalStr}`,
      pill: materialResidual ? "PARTLY EXPLAINED" : "SALE",
      breakdownLine: materialResidual
        ? `${saleNames} sale explains ~${formatUsd(matched)}; ${formatUsd(Math.abs(residual))} unexplained — review & allocate`
        : `includes a ${saleNames} sale — to allocate`,
    };
  }

  const sourceWord = salarySource(e.classified_source);
  if (sourceWord) {
    return {
      tone: "info",
      headline: `New cash from ${sourceWord}: ${totalStr}`,
      pill: sourceWord.toUpperCase(),
      breakdownLine: "to allocate",
    };
  }

  return {
    tone: "warn",
    headline: `Unexplained cash change of ${totalStr}`,
    pill: "UNEXPLAINED",
    breakdownLine:
      "no matching sale this month — likely a reallocation or sale; review & allocate",
  };
}

// The TS union is rsu_sale|stock_sale|unclear today, but the backend
// enum (salary|bonus|sale|other|unclassified|unclear) may widen it. Match
// defensively on the string so salary/bonus get human copy if they arrive.
function salarySource(source: WindfallClassifiedSource): string | null {
  const s = String(source);
  if (s === "salary") return "salary";
  if (s === "bonus") return "a bonus";
  return null;
}

function formatUsd(value: number): string {
  if (!Number.isFinite(value)) return "$—";
  return `$${Math.round(value).toLocaleString()}`;
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
