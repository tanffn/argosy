"use client";

import type { ReactNode } from "react";

import type { PortfolioPosition, PositionThesisDTO } from "@/lib/api";

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
  const reviewed = thesis?.last_fleet_check_at
    ? new Date(thesis.last_fleet_check_at).toLocaleDateString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
      })
    : null;
  const estate =
    position.estate_safe === true
      ? "Estate-safe (non-US-situs)"
      : position.estate_safe === false
        ? "US-situs"
        : isCash
          ? "Cash (portfolio-interest exempt)"
          : "Estate unknown";

  return (
    <span className="group relative inline-block">
      {children}
      <span
        role="tooltip"
        className="pointer-events-none absolute left-0 top-full z-30 mt-1 hidden w-72 rounded-md border border-border bg-popover p-3 text-left text-[11px] text-popover-foreground shadow-md group-hover:block"
      >
        <div className="font-semibold text-sm">
          {position.symbol || "—"}
          {position.name ? (
            <span className="font-normal text-muted-foreground"> · {position.name}</span>
          ) : null}
        </div>
        {position.what_it_is ? (
          <p className="mt-1.5 text-muted-foreground">{position.what_it_is}</p>
        ) : (
          <p className="mt-1.5 text-muted-foreground/70 italic">No what-it-is blurb yet</p>
        )}
        {position.why_held ? (
          <p className="mt-1 text-muted-foreground">
            <span className="font-medium text-foreground">Why held: </span>
            {position.why_held}
          </p>
        ) : null}
        <div className="mt-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-muted-foreground">
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
            {position.usd_value_k != null ? `$${position.usd_value_k.toLocaleString()}K` : "—"}
          </span>
        </div>
      </span>
    </span>
  );
}
