"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import type { PortfolioPosition, PositionThesisDTO } from "@/lib/api";

/** Shared popover shell — Symbol and Verdict hovers must match visually. */
export function HoverPanel({
  children,
  panel,
  align = "left",
}: {
  children: ReactNode;
  panel: ReactNode;
  align?: "left" | "right";
}) {
  return (
    <span className="group relative inline-block">
      {children}
      <span
        role="tooltip"
        className={`pointer-events-none absolute top-full z-30 mt-1 hidden w-72 rounded-md border border-border bg-popover p-3 text-left text-[11px] font-normal text-popover-foreground shadow-md group-hover:block ${
          align === "right" ? "right-0" : "left-0"
        }`}
      >
        {panel}
      </span>
    </span>
  );
}

function formatReviewedAt(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/**
 * Hover card for one holding — name, what/why (stored blurbs), sleeve,
 * estate, last verdict. Blurbs are authored at seed time, not per render.
 */
export function HoldingHoverCard({
  position,
  thesis,
  children,
}: {
  position: PortfolioPosition;
  thesis?: PositionThesisDTO;
  children: ReactNode;
}) {
  const isCash = (position.asset_type || "").toLowerCase() === "cash";
  const sleeve = (position.sleeve || position.type_label || position.asset_type || "").trim();
  const reviewed = formatReviewedAt(thesis?.last_fleet_check_at);
  const estate =
    position.estate_safe === true
      ? "Estate-safe (non-US-situs)"
      : position.estate_safe === false
        ? "US-situs"
        : isCash
          ? "Cash (portfolio-interest exempt)"
          : "Estate unknown";

  return (
    <HoverPanel
      panel={
        <>
          <div className="font-semibold text-sm font-sans">
            {position.symbol || "—"}
            {position.name ? (
              <span className="font-normal text-muted-foreground"> · {position.name}</span>
            ) : null}
          </div>
          {position.what_it_is ? (
            <p className="mt-1.5 text-muted-foreground font-sans">{position.what_it_is}</p>
          ) : (
            <p className="mt-1.5 text-muted-foreground/70 italic font-sans">
              No what-it-is blurb yet
            </p>
          )}
          {position.why_held ? (
            <p className="mt-1 text-muted-foreground font-sans">
              <span className="font-medium text-foreground">Why held: </span>
              {position.why_held}
            </p>
          ) : null}
          <div className="mt-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-muted-foreground font-sans">
            <span className="font-medium text-foreground">Sleeve</span>
            <span>{sleeve || "—"}</span>
            <span className="font-medium text-foreground">Estate</span>
            <span>{estate}</span>
            <span className="font-medium text-foreground">Source</span>
            <span>{position.classification_source || "—"}</span>
            <span className="font-medium text-foreground">Verdict</span>
            <span>
              {thesis
                ? `${thesis.verdict}${reviewed ? ` · reviewed ${reviewed}` : " · undated"}`
                : "—"}
            </span>
            <span className="font-medium text-foreground">Value</span>
            <span className="tabular-nums">
              {position.usd_value_k != null
                ? `$${position.usd_value_k.toLocaleString()}K`
                : "—"}
            </span>
          </div>
        </>
      }
    >
      {children}
    </HoverPanel>
  );
}

const VERDICT_CLASS: Record<PositionThesisDTO["verdict"], string> = {
  HOLD: "text-muted-foreground border-border/40 bg-secondary/40",
  BUY: "text-emerald-400 border-emerald-400/40 bg-emerald-400/10",
  ADD: "text-emerald-400 border-emerald-400/40 bg-emerald-400/10",
  TRIM: "text-amber-400 border-amber-400/40 bg-amber-400/10",
  SELL: "text-rose-400 border-rose-400/40 bg-rose-400/10",
};

/** Verdict chip with the same hover panel chrome as Symbol. */
export function VerdictHoverCard({
  thesis,
}: {
  thesis: PositionThesisDTO;
}) {
  const reviewed = formatReviewedAt(thesis.last_fleet_check_at);
  const falsifierNote =
    thesis.falsifier_state === "none_recorded"
      ? "⚠ no falsifiers recorded"
      : `falsifiers ${thesis.falsifier_state ?? "—"}`;

  return (
    <HoverPanel
      align="right"
      panel={
        <>
          <div className="font-semibold text-sm font-sans">
            {thesis.verdict}
            <span className="font-normal text-muted-foreground">
              {" "}
              · {thesis.conviction} conviction
            </span>
          </div>
          <div className="mt-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-muted-foreground font-sans">
            <span className="font-medium text-foreground">Reviewed</span>
            <span>{reviewed ?? "undated"}</span>
            <span className="font-medium text-foreground">Falsifiers</span>
            <span>{falsifierNote}</span>
          </div>
          {thesis.reasoning_md ? (
            <p className="mt-2 text-muted-foreground font-sans leading-snug">
              {thesis.reasoning_md.slice(0, 280)}
              {thesis.reasoning_md.length > 280 ? "…" : ""}
            </p>
          ) : null}
          <p className="mt-2 text-[10px] text-muted-foreground/70 font-sans">
            Open /positions for the full thesis
          </p>
        </>
      }
    >
      <Link
        href="/positions"
        className={`inline-block px-2 py-0.5 rounded border text-[10px] font-medium tabular-nums cursor-help hover:opacity-80 transition-opacity ${VERDICT_CLASS[thesis.verdict]}`}
      >
        {thesis.verdict}
        {thesis.falsifier_state === "none_recorded" ? " ⚠" : ""}
      </Link>
    </HoverPanel>
  );
}
