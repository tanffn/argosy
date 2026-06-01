"use client";

/**
 * Wave 8 Piece F — Actions timeline.
 *
 * A unified, date-sorted vertical timeline of every action the
 * synthesizer emitted across the three horizons (long / medium /
 * short), plus every non-pct target that the glidepath excluded
 * (per Piece B1's ``excluded_targets`` payload — so nothing falls
 * off the page just because it didn't fit the chart's percentage
 * lens).
 *
 * Rendering:
 *   - Dated actions: first, sorted by ISO date ascending.
 *   - Parameterized actions ("if VIX > 30 → accelerate"): appear
 *     below dated actions, grouped as "ongoing triggers" with
 *     the trigger expression.
 *   - Directional actions: appear last, grouped as "ongoing
 *     directional posture".
 *   - Excluded targets (non-% units): merged into the dated section
 *     keyed by their revisit_after.
 */

import { useMemo, useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type {
  AllocationGlidepathResponse,
  DraftResponse,
  ExcludedTargetDTO,
  HorizonView,
} from "@/lib/api";

interface ActionsTimelineProps {
  /** Structured current plan from /api/plan/current/structured. */
  structured: DraftResponse | null;
  /** Glidepath response (Piece B1) — used to surface excluded_targets
   *  as timeline rows. */
  glidepath: AllocationGlidepathResponse | null;
}

type ActionKind = "dated" | "parameterized" | "directional";

interface TimelineRow {
  source: "action" | "excluded_target";
  horizon: "long" | "medium" | "short";
  kind: ActionKind | "non_pct_target";
  label: string;
  dateIso: string | null;
  trigger: string | null;
  detail: string;
  rationale: string;
  citedSources: string[];
}

const ISO_DATE_RE = /^(\d{4})-(\d{2})(?:-(\d{2}))?/;

function parseIsoDate(s: string | null): Date | null {
  if (!s) return null;
  const m = ISO_DATE_RE.exec(s.trim());
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]) - 1;
  const day = m[3] ? Number(m[3]) : 1;
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) {
    return null;
  }
  return new Date(year, month, day);
}

function readActionsFromHorizon(
  h: HorizonView | null,
  horizonKey: "long" | "medium" | "short",
): TimelineRow[] {
  if (!h) return [];
  const out: TimelineRow[] = [];
  for (const raw of h.actions ?? []) {
    if (!raw || typeof raw !== "object") continue;
    const a = raw as Record<string, unknown>;
    const kind = (a.horizon_kind as ActionKind | undefined) ?? "directional";
    const triggerOrDate = (a.trigger_or_date as string | null | undefined) ?? null;
    const label = (a.label as string | undefined) ?? "(no label)";
    const detail = (a.detail as string | undefined) ?? "";
    const rationale = (a.rationale as string | undefined) ?? "";
    const cited = Array.isArray(a.cited_sources)
      ? (a.cited_sources as string[])
      : [];
    out.push({
      source: "action",
      horizon: horizonKey,
      kind,
      label,
      dateIso: kind === "dated" ? triggerOrDate : null,
      trigger: kind === "parameterized" ? triggerOrDate : null,
      detail,
      rationale,
      citedSources: cited,
    });
  }
  return out;
}

function readExcludedTargets(
  glidepath: AllocationGlidepathResponse | null,
): TimelineRow[] {
  if (!glidepath || !glidepath.excluded_targets) return [];
  return glidepath.excluded_targets.map((t: ExcludedTargetDTO) => ({
    source: "excluded_target" as const,
    horizon: "long" as const, // unknown — defaults to long for sorting stability
    kind: "non_pct_target" as const,
    label: `${t.target_label} (${formatTargetValue(t)})`,
    dateIso: t.target_date,
    trigger: null,
    detail: t.reason,
    rationale: "",
    citedSources: [],
  }));
}

function formatTargetValue(t: ExcludedTargetDTO): string {
  if (t.target_unit === "nis") {
    return `₪${t.target_value.toLocaleString()}`;
  }
  if (t.target_unit === "usd") {
    return `$${t.target_value.toLocaleString()}`;
  }
  return `${t.target_value} ${t.target_unit}`;
}

export function ActionsTimeline({
  structured,
  glidepath,
}: ActionsTimelineProps) {
  const rows = useMemo(() => {
    if (!structured) return [];
    const all: TimelineRow[] = [
      ...readActionsFromHorizon(structured.horizon_long, "long"),
      ...readActionsFromHorizon(structured.horizon_medium, "medium"),
      ...readActionsFromHorizon(structured.horizon_short, "short"),
      ...readExcludedTargets(glidepath),
    ];
    return all;
  }, [structured, glidepath]);

  const dated = useMemo(
    () =>
      rows
        .filter((r) => r.dateIso != null)
        .map((r) => ({ r, d: parseIsoDate(r.dateIso) }))
        .filter((p) => p.d != null)
        .sort((a, b) => (a.d!.getTime() - b.d!.getTime()))
        .map((p) => p.r),
    [rows],
  );
  const parameterized = useMemo(
    () => rows.filter((r) => r.kind === "parameterized" && !r.dateIso),
    [rows],
  );
  const directional = useMemo(
    () => rows.filter((r) => r.kind === "directional"),
    [rows],
  );

  const isEmpty =
    dated.length === 0 && parameterized.length === 0 && directional.length === 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Key actions</CardTitle>
        <CardDescription>
          Cross-horizon timeline of every dated action, parameterized
          trigger, and directional posture the synthesizer emitted —
          plus every non-percentage target the allocation glidepath
          couldn&apos;t place on its chart (so nothing gets dropped).
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isEmpty ? (
          <p className="text-sm text-muted-foreground py-2">
            The current plan has no actions or excluded targets to surface.
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            {dated.length > 0 ? (
              <TimelineSection title="Dated">
                {dated.map((r, i) => (
                  <TimelineRowBlock key={`d-${i}`} row={r} />
                ))}
              </TimelineSection>
            ) : null}
            {parameterized.length > 0 ? (
              <TimelineSection title="Ongoing triggers">
                {parameterized.map((r, i) => (
                  <TimelineRowBlock key={`p-${i}`} row={r} />
                ))}
              </TimelineSection>
            ) : null}
            {directional.length > 0 ? (
              <TimelineSection title="Directional posture">
                {directional.map((r, i) => (
                  <TimelineRowBlock key={`x-${i}`} row={r} />
                ))}
              </TimelineSection>
            ) : null}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function TimelineSection(props: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h4 className="text-[11px] uppercase tracking-wide text-muted-foreground mb-2">
        {props.title}
      </h4>
      <ul className="flex flex-col gap-2">{props.children}</ul>
    </section>
  );
}

function TimelineRowBlock({ row }: { row: TimelineRow }) {
  const [open, setOpen] = useState(false);
  const dateLabel = row.dateIso
    ? row.dateIso.length >= 10
      ? row.dateIso.slice(0, 10)
      : row.dateIso
    : null;
  return (
    <li className="border border-border/60 rounded-md p-2.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left flex items-start gap-2"
      >
        <span className="text-xs uppercase font-mono text-muted-foreground min-w-[55px]">
          [{row.horizon}]
        </span>
        {dateLabel ? (
          <span className="text-xs font-mono text-primary min-w-[85px]">
            {dateLabel}
          </span>
        ) : row.trigger ? (
          <span
            className="text-xs font-mono text-info min-w-[85px] truncate"
            title={row.trigger}
          >
            {row.trigger}
          </span>
        ) : (
          <span className="text-xs font-mono text-muted-foreground min-w-[85px]">
            ongoing
          </span>
        )}
        <span className="text-sm flex-1">{row.label}</span>
        {row.source === "excluded_target" ? (
          <Badge variant="outline" className="text-[10px]">
            target
          </Badge>
        ) : null}
      </button>
      {open ? (
        <div className="mt-2 text-xs flex flex-col gap-1 text-muted-foreground border-t border-border/40 pt-2">
          {row.detail ? <p>{row.detail}</p> : null}
          {row.rationale ? (
            <p>
              <span className="font-semibold">Why:</span> {row.rationale}
            </p>
          ) : null}
          {row.citedSources.length > 0 ? (
            <p className="text-[11px]">
              cite: {row.citedSources.join(", ")}
            </p>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
