"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  Briefcase,
  Heart,
  Home as HomeIcon,
  Banknote,
  Repeat,
  Flag,
  AlertCircle,
} from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type HolisticTimelineDTO,
  type LifeEventMarkerDTO,
  type RetireZoneDTO,
  type VestMarkerDTO,
} from "@/lib/api";

interface Props {
  userId: string;
}

/**
 * Sprint commit #10 — horizontal timeline visualization of the user's
 * upcoming RSU vests, structured life events, and bear/base/bull
 * retire-ready zones.
 *
 * Five overlay layers (per spec #1 §3):
 *   1. Past RSU vests       — filled green circles
 *   2. Future RSU vests     — outlined green circles
 *   3. Life events          — shape-aware markers per delta_kind
 *                             (one-shot dot / recurring fading-dots /
 *                             phase-change vertical line)
 *   4. Retire-ready zones   — three thin vertical stripes (bear/base/bull)
 *   5. Constraint labels    — text annotation when a zone is clamped by
 *                             rsu_unvested (the only surviving date
 *                             clamp after Spec D #3 dropped life_event)
 *
 * Today's date gets a dedicated "TODAY" vertical line. Each marker is
 * positioned by `(markerDate - rangeStart) / (rangeEnd - rangeStart) *
 * 100%`, so the timeline scales with container width.
 *
 * Spec D §5 (2026-05-29 life-events cashflow redesign) added the
 * delta_kind-aware marker mapping AND the "Undated events" sidebar
 * list for delta_kind='none' / no-date rows.
 *
 * Backend: GET /api/retirement/timeline?user_id=X&horizon_days=Y.
 */
export function HolisticTimelineCard({ userId }: Props) {
  const [horizonDays, setHorizonDays] = useState<number>(365 * 30);
  const [data, setData] = useState<HolisticTimelineDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.retirement
      .holisticTimeline(userId, horizonDays)
      .then((d) => {
        if (cancelled) return;
        setData(d);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId, horizonDays]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <CardTitle className="text-base">Holistic Timeline</CardTitle>
            <CardDescription className="mt-1">
              RSU vests, life events, and retire-ready zones on one axis.
            </CardDescription>
          </div>
          <HorizonSelector
            value={horizonDays}
            onChange={setHorizonDays}
          />
        </div>
      </CardHeader>
      <CardContent>
        {loading ? (
          <div className="text-sm text-muted-foreground">
            Loading timeline&hellip;
          </div>
        ) : error ? (
          <div className="text-sm text-rose-400">{error}</div>
        ) : data === null ? (
          <div className="text-sm text-muted-foreground">&mdash;</div>
        ) : (
          <TimelineBody data={data} />
        )}
      </CardContent>
    </Card>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Horizon selector — 10y / 30y toggle
// ─────────────────────────────────────────────────────────────────────────

interface HorizonSelectorProps {
  value: number;
  onChange: (v: number) => void;
}

function HorizonSelector({ value, onChange }: HorizonSelectorProps) {
  const options: Array<{ label: string; days: number }> = [
    { label: "10y", days: 365 * 10 },
    { label: "30y", days: 365 * 30 },
  ];
  return (
    <div className="inline-flex rounded-md border border-border bg-secondary/30 p-0.5 text-xs font-mono">
      {options.map((opt) => {
        const active = opt.days === value;
        return (
          <button
            key={opt.label}
            type="button"
            onClick={() => onChange(opt.days)}
            className={
              "px-2.5 py-1 rounded transition-colors " +
              (active
                ? "bg-foreground/10 text-foreground"
                : "text-muted-foreground hover:text-foreground")
            }
            aria-pressed={active}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Timeline body — empty-state vs rendered layers
// ─────────────────────────────────────────────────────────────────────────

interface TimelineBodyProps {
  data: HolisticTimelineDTO;
}

function TimelineBody({ data }: TimelineBodyProps) {
  const totalMarkers =
    data.past_vests.length +
    data.future_vests.length +
    data.life_events.length +
    data.retire_ready_zones.length;

  // Compute date range + axis ticks unconditionally — hooks must run on
  // every render regardless of the empty-state branch below.
  const range = useMemo(() => computeRange(data), [data]);
  const ticks = useMemo(() => buildAxisTicks(range), [range]);

  // Empty-state nudge — distinct from "API failed" so the user gets a
  // pointer to the two seeding surfaces (/life-events form +
  // Schwab CSV upload).
  if (totalMarkers === 0) {
    return (
      <div className="rounded-md border border-dashed border-border bg-secondary/20 px-4 py-6 text-sm text-muted-foreground">
        No timeline data yet. Add life events on{" "}
        <Link
          href="/life-events"
          className="text-info hover:underline"
        >
          /life-events
        </Link>{" "}
        to populate it, or upload a Schwab CSV to seed vest events.
      </div>
    );
  }

  const todayPct = pct(range, parseDateUtc(data.today));

  // Pull clamped-zone annotations out for the constraint-label row.
  // Spec D #3 removed the 'life_event' clamp_reason — the only date-
  // clamp constraint that survives is rsu_unvested (RSUs are a real
  // liquidity constraint; life events flow through cashflow instead).
  const clampedZones = data.retire_ready_zones.filter(
    (z) => z.clamp_reason === "rsu_unvested",
  );

  // Spec D §5 — split events by cashflow shape. `none`-kind events
  // (and any event missing every date field) render in the "Undated
  // events" sidebar instead of the timeline plane (per spec §7.6
  // test #3). Everything else maps onto the timeline per delta_kind.
  const datedEvents = data.life_events.filter((e) => !isUndated(e));
  const undatedEvents = data.life_events.filter(isUndated);

  return (
    <div className="space-y-3">
      <div className="overflow-x-auto">
        <div className="min-w-[640px]">
          {/* Track container — relative positioning so absolute markers
              layer over the same horizontal axis. Height accommodates
              two marker rows (vests above, life events below) + the
              zone stripes spanning full height. */}
          <div className="relative h-32 rounded-md border border-border bg-secondary/20">
            {/* Retire-ready zones — render FIRST so they sit underneath
                the vest/event markers. */}
            {data.retire_ready_zones.map((zone, i) => (
              <RetireZoneStripe
                key={`zone-${i}`}
                zone={zone}
                leftPct={pct(range, parseDateUtc(zone.expected_date))}
              />
            ))}

            {/* TODAY vertical line. */}
            <TodayLine leftPct={todayPct} />

            {/* Horizontal axis baseline. */}
            <div className="absolute left-0 right-0 top-1/2 h-px bg-border/70" />

            {/* Past vests — filled green circles, top half. */}
            {data.past_vests.map((v, i) => (
              <VestMarker
                key={`past-${i}`}
                vest={v}
                leftPct={pct(range, parseDateUtc(v.date))}
                top="22%"
              />
            ))}

            {/* Future vests — outlined green circles, top half. */}
            {data.future_vests.map((v, i) => (
              <VestMarker
                key={`future-${i}`}
                vest={v}
                leftPct={pct(range, parseDateUtc(v.date))}
                top="22%"
              />
            ))}

            {/* Life events — colored markers, bottom half.
                Per Spec D §5 the marker shape depends on delta_kind:
                  one_shot            -> single dot at one_shot_date / date
                  recurring_*         -> dots at each in-horizon occurrence,
                                         fading toward the horizon
                  phase_change_start  -> vertical line at phase_start_date
                                         (subtle "from" label)
                  phase_change_end    -> vertical line at phase_end_date
                                         (subtle "until" label)
                  none / undated      -> rendered in the sidebar list below,
                                         NOT on the timeline plane
                Legacy markers (no delta_kind from the current backend)
                fall through to the single-dot path. */}
            {datedEvents.map((e, i) => (
              <LifeEventShape
                key={`event-${i}`}
                event={e}
                range={range}
              />
            ))}
          </div>

          {/* Axis tick labels — short month + year. */}
          <div className="relative mt-1 h-4">
            {ticks.map((t, i) => (
              <div
                key={i}
                className="absolute -translate-x-1/2 text-[10px] font-mono text-muted-foreground"
                style={{ left: `${t.leftPct}%` }}
              >
                {t.label}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Retire-ready chips row — one chip per scenario. */}
      {data.retire_ready_zones.length > 0 ? (
        <div className="flex flex-wrap gap-2 pt-1">
          {data.retire_ready_zones.map((zone, i) => (
            <RetireZoneChip key={`chip-${i}`} zone={zone} />
          ))}
        </div>
      ) : null}

      {/* Constraint annotations — only when bear/base/bull is clamped. */}
      {clampedZones.length > 0 ? (
        <div className="space-y-1 text-xs text-muted-foreground">
          {clampedZones.map((z, i) => (
            <div key={`clamp-${i}`} className="flex items-start gap-1.5">
              <AlertCircle className="h-3 w-3 mt-0.5 text-rose-400/80" />
              <span>
                Earliest <span className="font-semibold">{z.scenario}</span> ={" "}
                <span className="font-mono">{z.expected_date}</span> &mdash;
                clamped by{" "}
                <span className="font-mono">{z.clamp_reason}</span>
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {/* Undated events sidebar — Spec D §5 + §7.6 test #3.
          delta_kind='none' rows (sigma_calibration, withdrawal_policy_change,
          legacy retire-year-change rows preserved by migration 0049) and any
          other event without a renderable date land here so they stay
          visible without sitting on the timeline plane. */}
      {undatedEvents.length > 0 ? (
        <div className="rounded-md border border-border bg-secondary/20 px-3 py-2">
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground mb-1">
            Undated events &middot;{" "}
            <span className="font-mono">{undatedEvents.length}</span>
          </div>
          <ul className="space-y-1 text-xs">
            {undatedEvents.map((e, i) => {
              const { Icon, colorClass } = categoryStyle(e.category);
              return (
                <li
                  key={`undated-${i}`}
                  className="flex items-start gap-1.5"
                  title={formatEventTooltip(e)}
                >
                  <Icon className={"h-3 w-3 mt-0.5 " + colorClass} />
                  <span className="text-muted-foreground">
                    <span className="font-mono">{e.category}/{e.kind}</span>
                    {e.description ? (
                      <span> &middot; {e.description}</span>
                    ) : null}
                    <span className="text-muted-foreground/70">
                      {" "}&middot; no cashflow effect
                    </span>
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      <div className="text-[11px] text-muted-foreground pt-1">
        Vests: <span className="font-mono">{data.past_vests.length}</span>{" "}
        past /{" "}
        <span className="font-mono">{data.future_vests.length}</span>{" "}
        future &middot; Life events:{" "}
        <span className="font-mono">{data.life_events.length}</span>
        {undatedEvents.length > 0 ? (
          <span className="text-muted-foreground/70">
            {" "}({undatedEvents.length} undated)
          </span>
        ) : null}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Marker components
// ─────────────────────────────────────────────────────────────────────────

interface VestMarkerProps {
  vest: VestMarkerDTO;
  leftPct: number;
  top: string;
}

function VestMarker({ vest, leftPct, top }: VestMarkerProps) {
  const isPast = vest.kind === "past_vest";
  const tooltip = formatVestTooltip(vest);
  return (
    <div
      className="absolute -translate-x-1/2 -translate-y-1/2 group"
      style={{ left: `${leftPct}%`, top }}
    >
      <div
        className={
          "h-2.5 w-2.5 rounded-full transition-transform group-hover:scale-150 " +
          (isPast
            ? "bg-emerald-500 border border-emerald-500"
            : "bg-transparent border-2 border-emerald-500")
        }
        title={tooltip}
        aria-label={tooltip}
      />
    </div>
  );
}

interface LifeEventDotProps {
  event: LifeEventMarkerDTO;
  leftPct: number;
  top: string;
  /** Visual de-emphasis for far-out recurring occurrences. 1.0 = full
   *  opacity; lower values fade toward the horizon (Spec D §5). */
  opacity?: number;
  /** Tooltip override — used to annotate which occurrence of a
   *  recurring series the dot represents. */
  tooltipOverride?: string;
}

function LifeEventDot({
  event,
  leftPct,
  top,
  opacity = 1,
  tooltipOverride,
}: LifeEventDotProps) {
  const { Icon, colorClass, isDown } = categoryStyle(event.category);
  const tooltip = tooltipOverride ?? formatEventTooltip(event);
  return (
    <div
      className="absolute -translate-x-1/2 -translate-y-1/2 group"
      style={{ left: `${leftPct}%`, top, opacity }}
      title={tooltip}
      aria-label={tooltip}
    >
      <Icon
        className={
          "h-3.5 w-3.5 transition-transform group-hover:scale-150 " +
          colorClass +
          (isDown ? " rotate-180" : "")
        }
      />
    </div>
  );
}

interface LifeEventPhaseLineProps {
  event: LifeEventMarkerDTO;
  leftPct: number;
  /** "from" for phase_change_start, "until" for phase_change_end. */
  edgeLabel: "from" | "until";
}

/** Vertical line marker for phase_change_start / phase_change_end shapes.
 *  Uses the same width/tone primitives as the retire-ready stripes to
 *  stay visually consistent. */
function LifeEventPhaseLine({
  event,
  leftPct,
  edgeLabel,
}: LifeEventPhaseLineProps) {
  const { colorClass } = categoryStyle(event.category);
  const tooltip = formatEventTooltip(event);
  // Translate the tailwind text-color class into a matching background
  // tint for the vertical line. categoryStyle ships text-* classes
  // because the existing icons use those; lines need a bg-* sibling.
  // Both lookups go through the static CATEGORY_BG map so Tailwind v4's
  // source scanner can see the bg-* class names verbatim.
  const bgTone = textToBg(colorClass);
  return (
    <div
      className="absolute top-0 bottom-0 pointer-events-none"
      style={{ left: `${leftPct}%` }}
      title={tooltip}
      aria-label={tooltip}
    >
      <div className={"w-[2px] h-full " + bgTone + " opacity-70"} />
      <div
        className={
          "absolute bottom-0 -translate-x-1/2 px-1 py-px rounded-sm " +
          "text-[9px] font-mono uppercase tracking-wider " +
          colorClass +
          " bg-secondary/40"
        }
      >
        {edgeLabel}
      </div>
    </div>
  );
}

interface LifeEventShapeProps {
  event: LifeEventMarkerDTO;
  range: DateRange;
}

/** Spec D §5 — dispatches each life-event marker to the right visual
 *  primitive based on `delta_kind`. Events without a usable date are
 *  filtered out upstream and render in the sidebar instead. */
function LifeEventShape({ event, range }: LifeEventShapeProps) {
  const TOP = "72%";
  const dk = event.delta_kind;

  // recurring_every_n_years — one dot per in-horizon occurrence,
  // fading toward the horizon edge so the eye groups them as a series.
  if (dk === "recurring_every_n_years") {
    const dates = (event.recurring_dates ?? []).filter(Boolean);
    if (dates.length === 0) {
      // Backend hasn't expanded yet; degrade to a single anchor dot.
      const anchor = event.date ?? null;
      if (!anchor) return null;
      return (
        <LifeEventDot
          event={event}
          leftPct={pct(range, parseDateUtc(anchor))}
          top={TOP}
        />
      );
    }
    return (
      <>
        {dates.map((d, i) => {
          // Linear fade from 1.0 (first occurrence) to 0.45 (last) so
          // far-future recurrences read as "still scheduled, less
          // certain" without disappearing entirely.
          const t = dates.length === 1 ? 0 : i / (dates.length - 1);
          const opacity = 1 - 0.55 * t;
          const amt =
            event.recurring_amount_usd ??
            event.amount_usd ??
            null;
          const amtLabel =
            amt !== null ? `$${(amt / 1000).toFixed(1)}K` : "—";
          const tooltip =
            `${event.category}/${event.kind} · recurring #${i + 1}` +
            ` · ${d} · ${amtLabel}`;
          return (
            <LifeEventDot
              key={`rec-${i}`}
              event={event}
              leftPct={pct(range, parseDateUtc(d))}
              top={TOP}
              opacity={opacity}
              tooltipOverride={tooltip}
            />
          );
        })}
      </>
    );
  }

  // phase_change_start — vertical line at phase_start_date with
  // subtle "from" label.
  if (dk === "phase_change_start") {
    const startDate =
      event.phase_start_date ?? event.date ?? null;
    if (!startDate) return null;
    return (
      <LifeEventPhaseLine
        event={event}
        leftPct={pct(range, parseDateUtc(startDate))}
        edgeLabel="from"
      />
    );
  }

  // phase_change_end — vertical line at phase_end_date with subtle
  // "until" label. The matching start line is implicit (renders only
  // when start_date is also a separate row, per Spec D's row model).
  if (dk === "phase_change_end") {
    const endDate =
      event.phase_end_date ?? event.phase_start_date ?? event.date ?? null;
    if (!endDate) return null;
    return (
      <LifeEventPhaseLine
        event={event}
        leftPct={pct(range, parseDateUtc(endDate))}
        edgeLabel="until"
      />
    );
  }

  // one_shot OR legacy (no delta_kind) — single dot at the event date.
  const oneShotDate =
    event.one_shot_date ?? event.date ?? null;
  if (!oneShotDate) return null;
  return (
    <LifeEventDot
      event={event}
      leftPct={pct(range, parseDateUtc(oneShotDate))}
      top={TOP}
    />
  );
}

interface RetireZoneStripeProps {
  zone: RetireZoneDTO;
  leftPct: number;
}

function RetireZoneStripe({ zone, leftPct }: RetireZoneStripeProps) {
  const tint =
    zone.scenario === "bear"
      ? "bg-rose-400/70"
      : zone.scenario === "base"
        ? "bg-emerald-500/80"
        : "bg-indigo-400/70";
  const tooltip = `${zone.scenario.toUpperCase()} retire-ready: ${zone.expected_date} (age ${zone.age_years.toFixed(1)})`;
  return (
    <div
      className={"absolute top-0 bottom-0 w-[2px] " + tint}
      style={{ left: `${leftPct}%` }}
      title={tooltip}
      aria-label={tooltip}
    />
  );
}

interface RetireZoneChipProps {
  zone: RetireZoneDTO;
}

function RetireZoneChip({ zone }: RetireZoneChipProps) {
  const tone =
    zone.scenario === "bear"
      ? "border-rose-400/40 bg-rose-400/10 text-rose-300"
      : zone.scenario === "base"
        ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
        : "border-indigo-400/40 bg-indigo-400/10 text-indigo-300";
  return (
    <span
      className={
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-mono " +
        tone
      }
    >
      <span className="uppercase tracking-wide">{zone.scenario}</span>
      <span>retire-ready: {formatMonthYear(parseDateUtc(zone.expected_date))}</span>
      <span className="text-muted-foreground">
        (age {zone.age_years.toFixed(1)})
      </span>
    </span>
  );
}

interface TodayLineProps {
  leftPct: number;
}

function TodayLine({ leftPct }: TodayLineProps) {
  if (leftPct < 0 || leftPct > 100) return null;
  return (
    <div
      className="absolute top-0 bottom-0 pointer-events-none"
      style={{ left: `${leftPct}%` }}
    >
      <div className="w-px h-full bg-amber-400/80" />
      <div className="absolute top-0 -translate-x-1/2 px-1 py-px rounded-sm bg-amber-400/15 text-amber-300 text-[9px] font-mono uppercase tracking-wider">
        Today
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Helpers — date math + formatting + category style table
// ─────────────────────────────────────────────────────────────────────────

interface DateRange {
  startMs: number;
  endMs: number;
}

/** Parse an ISO YYYY-MM-DD as UTC midnight. Avoid `new Date(iso)` because
 *  that interprets bare-date strings as local time in some engines, which
 *  shifts position by a few hours and can flip month boundaries on the
 *  axis. */
function parseDateUtc(iso: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return Date.parse(iso);
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

function pct(range: DateRange, ms: number): number {
  const span = range.endMs - range.startMs;
  if (span <= 0) return 0;
  return Math.max(0, Math.min(100, ((ms - range.startMs) / span) * 100));
}

function computeRange(data: HolisticTimelineDTO): DateRange {
  const allMs: number[] = [parseDateUtc(data.today)];
  for (const v of data.past_vests) allMs.push(parseDateUtc(v.date));
  for (const v of data.future_vests) allMs.push(parseDateUtc(v.date));
  for (const e of data.life_events) {
    // Spec D §5 — markers can carry per-shape dates instead of (or in
    // addition to) the legacy `date` field. Pull every renderable date
    // into the range so phase-change ends + recurring tails stretch
    // the axis correctly.
    for (const d of eventRenderDates(e)) allMs.push(parseDateUtc(d));
  }
  for (const z of data.retire_ready_zones)
    allMs.push(parseDateUtc(z.expected_date));
  const minMs = Math.min(...allMs);
  const maxMs = Math.max(...allMs);
  // ~1mo padding on each end so markers don't touch the container edge.
  // If start==end (single point), expand to a 2-year window around it.
  const padMs = 30 * 24 * 3600 * 1000;
  if (maxMs - minMs < padMs) {
    const year = 365 * 24 * 3600 * 1000;
    return { startMs: minMs - year, endMs: maxMs + year };
  }
  return { startMs: minMs - padMs, endMs: maxMs + padMs };
}

/** Every date the marker would render on the timeline plane. Used by
 *  the range computation AND the dated/undated split. Empty list means
 *  the event has no renderable date (delta_kind='none' with no anchor)
 *  — it goes in the sidebar list. */
function eventRenderDates(e: LifeEventMarkerDTO): string[] {
  const dk = e.delta_kind;
  if (dk === "recurring_every_n_years") {
    const dates = (e.recurring_dates ?? []).filter(
      (d): d is string => Boolean(d),
    );
    if (dates.length > 0) return dates;
    // Backend hasn't expanded the recurrence yet — anchor only.
    return e.date ? [e.date] : [];
  }
  if (dk === "phase_change_start") {
    const d = e.phase_start_date ?? e.date ?? null;
    return d ? [d] : [];
  }
  if (dk === "phase_change_end") {
    const d = e.phase_end_date ?? e.phase_start_date ?? e.date ?? null;
    return d ? [d] : [];
  }
  if (dk === "none") return [];
  // one_shot OR legacy (no delta_kind).
  const d = e.one_shot_date ?? e.date ?? null;
  return d ? [d] : [];
}

/** Sidebar/timeline split — undated when the marker has no renderable
 *  date OR when delta_kind is explicitly 'none'. */
function isUndated(e: LifeEventMarkerDTO): boolean {
  if (e.delta_kind === "none") return true;
  return eventRenderDates(e).length === 0;
}

/** Static text→bg mapping for the per-category tones used by
 *  categoryStyle(). Tailwind v4's JIT-style scanner only sees literal
 *  class strings in source, so the bg-* siblings of categoryStyle's
 *  text-* classes must appear here verbatim — a runtime
 *  `"bg-" + textClass.slice(5)` would tree-shake to nothing.
 *
 *  Keep CATEGORY_BG entries in sync with categoryStyle() below. */
const CATEGORY_BG: Record<string, string> = {
  "text-blue-400": "bg-blue-400",
  "text-purple-400": "bg-purple-400",
  "text-teal-400": "bg-teal-400",
  "text-rose-400": "bg-rose-400",
  "text-amber-400": "bg-amber-400",
  "text-indigo-400": "bg-indigo-400",
  "text-muted-foreground": "bg-muted-foreground",
};

function textToBg(textClass: string): string {
  return CATEGORY_BG[textClass] ?? "bg-muted-foreground";
}

interface AxisTick {
  leftPct: number;
  label: string;
}

function buildAxisTicks(range: DateRange): AxisTick[] {
  // Five equally-spaced labels — enough to anchor the eye without
  // crowding the row at 640px min-width.
  const positions = [0, 0.25, 0.5, 0.75, 1.0];
  return positions.map((p) => {
    const ms = range.startMs + p * (range.endMs - range.startMs);
    return { leftPct: p * 100, label: formatMonthYear(ms) };
  });
}

function formatMonthYear(ms: number): string {
  const d = new Date(ms);
  // toLocaleString without a locale arg picks the runtime default, which
  // can drift between Node SSR and the browser. Lock to en-US for
  // SSR/CSR stability.
  return d.toLocaleString("en-US", {
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

function formatVestTooltip(v: VestMarkerDTO): string {
  const kindLabel = v.kind === "past_vest" ? "Past vest" : "Future vest";
  const gross =
    v.estimated_gross_usd !== null
      ? `$${(v.estimated_gross_usd / 1000).toFixed(1)}K`
      : "—";
  return `${kindLabel} · ${v.date} · ${v.symbol} · ${v.shares} sh · ${gross}`;
}

function formatEventTooltip(e: LifeEventMarkerDTO): string {
  // Prefer the shape-specific signed amount (Spec D §1.2) when
  // present so the tooltip reads in the new model; fall back to the
  // legacy unsigned `amount_usd` otherwise.
  const rawAmt =
    e.one_shot_amount_usd ??
    e.recurring_amount_usd ??
    e.monthly_delta_usd ??
    e.amount_usd ??
    null;
  const amt =
    rawAmt !== null ? `$${(rawAmt / 1000).toFixed(1)}K` : "—";
  const desc = e.description ?? "—";
  // Pick the most-relevant render date for the tooltip; falls back to
  // a literal "(undated)" for delta_kind='none' rows.
  const dates = eventRenderDates(e);
  const dateLabel = dates.length > 0 ? dates[0] : "(undated)";
  return `${e.category}/${e.kind} · ${dateLabel} · ${amt} · ${desc}`;
}

interface CategoryStyle {
  Icon: React.ComponentType<{ className?: string }>;
  /** Tailwind text-color class for the icon glyph. */
  colorClass: string;
  /** Expense category renders as a down-pointing marker per spec. */
  isDown: boolean;
}

/** Map the LifeEventMarker.category string to icon + tone.
 *  Categories sourced from argosy/services/life_events.py:
 *    career_event, family_event, asset_event, expense_event,
 *    recurring_expense, retirement_milestone.
 *  The backend sometimes shortens these to bare names (`career`,
 *  `family`, etc.) on the marker payload — handle both. */
function categoryStyle(category: string): CategoryStyle {
  const c = category.toLowerCase();
  if (c.startsWith("career"))
    return { Icon: Briefcase, colorClass: "text-blue-400", isDown: false };
  if (c.startsWith("family"))
    return { Icon: Heart, colorClass: "text-purple-400", isDown: false };
  if (c.startsWith("asset"))
    return { Icon: HomeIcon, colorClass: "text-teal-400", isDown: false };
  if (c.startsWith("expense"))
    return { Icon: Banknote, colorClass: "text-rose-400", isDown: true };
  if (c.startsWith("recurring"))
    return { Icon: Repeat, colorClass: "text-amber-400", isDown: false };
  if (c.startsWith("retirement"))
    return { Icon: Flag, colorClass: "text-indigo-400", isDown: false };
  // Fallback — unknown category renders as a neutral grey dot icon.
  return { Icon: AlertCircle, colorClass: "text-muted-foreground", isDown: false };
}
