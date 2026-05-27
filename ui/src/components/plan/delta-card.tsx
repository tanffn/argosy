"use client";

import { useState } from "react";
import { Check, History, MessageSquareWarning, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  api,
  type DeltaItem,
  type FMObjection,
  type TargetProgress,
} from "@/lib/api";

interface DeltaCardProps {
  delta: DeltaItem;
  userId: string;
  disabled?: boolean;
  onAccept?: (delta: DeltaItem) => void | Promise<void>;
  onReject?: (delta: DeltaItem) => void | Promise<void>;
  onPushBack?: (delta: DeltaItem) => void | Promise<void>;
  onSourceClick?: (agentLabel: string) => void;
  // T4.3 — when a slim re-debate is in flight for this delta, the
  // parent passes the backend ``decision_run_id`` + status so we can
  // render an inline "Re-debate running…" pill and a link to
  // /decisions/<id> for the verdict trail.
  pushbackRun?: {
    decisionRunId: number;
    status: "running" | "completed" | "failed";
  } | null;
  // Prior-round FM objections (from
  // /api/plan/draft/objections::prior_round_objections). When the
  // delta's rationale references "Blocker #N" / "BLOCKER N" /
  // "Objection #N", we look up entry [N-1] here so the user can hover
  // over the chip and see the actual prior-round objection text
  // instead of staring at a bare number.
  priorRoundObjections?: FMObjection[];
  // Live "current vs target" annotation for TARGET item_kind. The /plan
  // page fetches GET /api/plan/draft/target-progress once and passes the
  // matching row in here so each TARGET card renders a thin strip below
  // the rationale showing current value + gap + status. Null/undefined
  // when the row is for a non-target delta OR the backend couldn't
  // compute progress for this item.
  targetProgress?: TargetProgress | null;
}

interface HistoryEntry {
  plan_version_id: number;
  version_label: string | null;
  role: string;
  drafted_at: string;
  horizon: string;
  label: string;
  value: number | string | null;
  unit: string | null;
  rationale: string;
  accepted: boolean;
}

function changeKindBadge(kind: DeltaItem["change_kind"]) {
  switch (kind) {
    case "added":
      return { variant: "success" as const, label: "ADD" };
    case "modified":
      return { variant: "secondary" as const, label: "CHANGE" };
    case "removed":
      return { variant: "error" as const, label: "REMOVE" };
  }
}

function itemKindLabel(kind: DeltaItem["item_kind"]): string {
  return kind.replace("_", " ").toUpperCase();
}

// Format a proposed/prior payload into the "<value> <unit>" headline used at
// the top of each card. Strips noisy keys (label/rationale/source_section)
// since the card already shows label + rationale separately.
function formatTargetValue(p: Record<string, unknown> | null): string | null {
  if (!p) return null;
  const value = p.value;
  const unit = (p.unit as string | undefined) ?? "";
  if (typeof value === "number") {
    if (unit.includes("pct")) return `${value}%`;
    if (unit.includes("usd") || unit === "$") return `$${value.toLocaleString()}`;
    if (unit) return `${value.toLocaleString()} ${unit}`;
    return String(value);
  }
  if (typeof value === "string" && value) {
    return unit ? `${value} ${unit}` : value;
  }
  // Action shape: { when, ticker, side, qty }
  const parts: string[] = [];
  if (typeof p.side === "string") parts.push(p.side.toUpperCase());
  if (typeof p.qty === "number" || typeof p.qty === "string") parts.push(String(p.qty));
  if (typeof p.ticker === "string") parts.push(p.ticker);
  if (typeof p.when === "string") parts.push(`(${p.when})`);
  return parts.length > 0 ? parts.join(" ") : null;
}

function proposedLabel(p: Record<string, unknown> | null): string | null {
  if (!p || typeof p !== "object") return null;
  const lbl = p.label;
  return typeof lbl === "string" && lbl ? lbl : null;
}

// Bug 1: detect whether a delta's prior + proposed describe IDENTICAL
// scalar values so the card can label it as "Rationale updated — value
// unchanged at X" instead of the misleading "suggested 45% before 45%".
//
// Compares the four primitive value-bearing keys the synthesizer uses
// across {target, action, theme} delta shapes: ``value``, ``side``,
// ``qty``, ``ticker``, ``when``, ``rule``, ``trigger``.  Equality is
// JSON-stable: ``45 === 45`` true; ``"45" === 45`` false; objects/arrays
// compared via stringify so structural equality is exact.
//
// Returns true only when BOTH payloads exist AND every comparable scalar
// matches — null/missing on one side counts as "not unchanged" so the
// "New target — X" branch (Bug 1 sub-case) gets to render its banner.
function isValueUnchanged(
  prior: Record<string, unknown> | null,
  proposed: Record<string, unknown> | null,
): boolean {
  if (!prior || !proposed) return false;
  const comparable = ["value", "side", "qty", "ticker", "when", "rule", "trigger"];
  let anyComparableKeyPresent = false;
  for (const k of comparable) {
    const a = prior[k];
    const b = proposed[k];
    if (a === undefined && b === undefined) continue;
    anyComparableKeyPresent = true;
    if (typeof a !== typeof b) return false;
    if (a === null || b === null) {
      if (a !== b) return false;
      continue;
    }
    if (typeof a === "object" || typeof b === "object") {
      if (JSON.stringify(a) !== JSON.stringify(b)) return false;
      continue;
    }
    if (a !== b) return false;
  }
  return anyComparableKeyPresent;
}

// Bug 2: parse a delta rationale for "Blocker #N" / "BLOCKER N" /
// "Objection #N" tokens and return the matched ranges so the caller can
// splice in <button> chips that surface the prior-round objection text.
//
// Pattern is intentionally generous: case-insensitive, optional '#',
// hyphenated alternates supported ("blocker-3" / "objection 3"). The
// returned segments alternate text/match in source order — a renderer
// can map() over them and emit a span or a chip per segment.
interface RationaleSegment {
  kind: "text" | "ref";
  text: string;
  refNumber?: number; // 1-based; only set when kind === "ref"
}

function parseRationaleReferences(rationale: string): RationaleSegment[] {
  if (!rationale) return [{ kind: "text", text: "" }];
  // \bBlocker\b or \bObjection\b followed by optional space/hyphen/'#'
  // and a 1-2 digit number. We capture the digits so we can resolve to
  // prior_round_objections[N-1].
  const pattern = /\b(blocker|objection)[\s#-]*(\d{1,2})\b/gi;
  const out: RationaleSegment[] = [];
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(rationale)) !== null) {
    if (m.index > lastIndex) {
      out.push({
        kind: "text",
        text: rationale.slice(lastIndex, m.index),
      });
    }
    const n = parseInt(m[2], 10);
    out.push({
      kind: "ref",
      text: m[0],
      refNumber: Number.isFinite(n) ? n : undefined,
    });
    lastIndex = m.index + m[0].length;
  }
  if (lastIndex < rationale.length) {
    out.push({ kind: "text", text: rationale.slice(lastIndex) });
  }
  if (out.length === 0) {
    out.push({ kind: "text", text: rationale });
  }
  return out;
}

// Format the live current_value the same way we format target values so
// "LIVE: NVDA 60.5%" vs "suggested 45%" sit on the same visual axis.
function formatLiveValue(
  value: number | null,
  unit: string,
): string {
  if (value === null || Number.isNaN(value)) return "—";
  const u = unit.toLowerCase();
  if (u.includes("pct")) return `${value.toFixed(1)}%`;
  if (u === "usd" || u === "$") return `$${Math.round(value).toLocaleString()}`;
  if (u === "nis") return `₪${Math.round(value).toLocaleString()}`;
  if (u === "shares") return `${Math.round(value).toLocaleString()} sh`;
  if (u === "months") return `${value.toFixed(1)} mo`;
  return `${value.toLocaleString()} ${unit}`;
}

// Format the gap as "+15.5pp" / "-200 sh" / "+$10,000". Pct units render
// percentage-points (pp); everything else uses the unit's standard suffix.
function formatGap(gap: number | null, unit: string): string {
  if (gap === null || Number.isNaN(gap)) return "—";
  const u = unit.toLowerCase();
  const sign = gap > 0 ? "+" : gap < 0 ? "" : "";
  if (u.includes("pct")) return `${sign}${gap.toFixed(1)}pp`;
  if (u === "usd" || u === "$") return `${sign}$${Math.round(gap).toLocaleString()}`;
  if (u === "nis") return `${sign}₪${Math.round(gap).toLocaleString()}`;
  if (u === "shares") return `${sign}${Math.round(gap).toLocaleString()} sh`;
  if (u === "months") return `${sign}${gap.toFixed(1)} mo`;
  return `${sign}${gap.toLocaleString()} ${unit}`;
}

// Pick the colour token + emoji + headline word for a TargetProgress
// status. Mapping per spec:
//   🟢 AT_TARGET (within ±2% relative)
//   🟡 close-but-off: BELOW for floor-style (direction_is_good=True) OR
//      ABOVE for ceiling-style (direction_is_good=False) when within
//      ~10% of target
//   🔴 wrong-direction: BELOW for floor / ABOVE for ceiling (beyond ~10%)
//   ⚪ UNKNOWN or ambiguous direction
function progressStripStyle(progress: TargetProgress): {
  tone: "success" | "warning" | "error" | "muted";
  glyph: string;
  headline: string;
} {
  if (progress.status === "AT_TARGET") {
    return { tone: "success", glyph: "●", headline: "at target" };
  }
  if (progress.status === "UNKNOWN" || progress.direction_is_good === null) {
    return { tone: "muted", glyph: "○", headline: "live state unavailable" };
  }
  // Ceiling (direction_is_good=false) → above is bad.
  // Floor   (direction_is_good=true)  → below is bad.
  const wrongDirection =
    (progress.status === "ABOVE_TARGET" && progress.direction_is_good === false) ||
    (progress.status === "BELOW_TARGET" && progress.direction_is_good === true);
  // Close-to-target threshold: |gap_pct| within 10% (relative) of target.
  const closeToTarget =
    progress.gap_pct !== null && Math.abs(progress.gap_pct) <= 10.0;
  if (wrongDirection && !closeToTarget) {
    return {
      tone: "error",
      glyph: "●",
      headline:
        progress.status === "ABOVE_TARGET" ? "above target" : "below target",
    };
  }
  if (wrongDirection && closeToTarget) {
    return {
      tone: "warning",
      glyph: "●",
      headline:
        progress.status === "ABOVE_TARGET" ? "above target" : "below target",
    };
  }
  // Right direction (e.g. ABOVE a floor or BELOW a ceiling) — always good.
  return { tone: "success", glyph: "●", headline: "on track" };
}

export function DeltaCard(props: DeltaCardProps) {
  const {
    delta,
    userId,
    disabled,
    onAccept,
    onReject,
    onPushBack,
    onSourceClick,
    pushbackRun,
    priorRoundObjections,
    targetProgress,
  } = props;
  const [rejectedLocally, setRejectedLocally] = useState(false);
  // Bug 2: chip popovers — keyed by "<segmentIndex>" so each rendered
  // chip toggles independently. Click toggles, blur closes.
  const [openRefIdx, setOpenRefIdx] = useState<number | null>(null);
  const [history, setHistory] = useState<
    HistoryEntry[] | "loading" | "error" | null
  >(null);

  const loadHistory = async () => {
    setHistory("loading");
    try {
      const r = await api.planItemHistory(userId, delta.item_id);
      setHistory(r.entries);
    } catch {
      setHistory("error");
    }
  };

  const toggleHistory = () => {
    if (history === null || history === "error") {
      void loadHistory();
    } else {
      setHistory(null);
    }
  };
  const badge = changeKindBadge(delta.change_kind);

  const propValue = formatTargetValue(delta.proposed);
  const propLabel = proposedLabel(delta.proposed);
  const priorValue = formatTargetValue(delta.prior);
  const labels = delta.provenance_agent_labels ?? [];
  // Bug 1 — compute the two no-op cases up front so the JSX block below
  // can branch cleanly. ``valueUnchanged`` is the "synthesizer flagged
  // this as modified but the scalar didn't move" case (e.g. only
  // rationale changed); ``isNewTarget`` is the "prior is null, proposed
  // is the first value for this item" case so we don't render
  // "suggested X before null" / "suggested X before —".
  const valueUnchanged = isValueUnchanged(delta.prior, delta.proposed);
  const isNewTarget = delta.prior === null && propValue !== null;

  // Bug 2 — segment the rationale on "Blocker #N" / "Objection #N"
  // tokens so we can substitute clickable chips. priorRoundObjections is
  // the list returned by /api/plan/draft/objections — N maps to entry
  // [N-1]. When the synthesizer hallucinated a number with no matching
  // prior objection, we still render the chip but its hover text says
  // "no matching prior objection found" (defensive, never crashes).
  const rationaleSegments = parseRationaleReferences(delta.rationale || "");
  const priorObj = priorRoundObjections ?? [];

  // Backend stores REJECTED / PUSHBACK in user_edit_note with a prefix. Parse
  // those so the card surfaces persistent state after a refresh, not just the
  // local-React click-state.
  const editNote = delta.user_edit_note ?? "";
  const persistedRejected = editNote.startsWith("REJECTED");
  const pushbackLines = editNote
    .split("\n")
    .filter((l) => l.startsWith("PUSHBACK:"))
    .map((l) => l.slice("PUSHBACK:".length).trim());

  const isAccepted = delta.accepted;
  const isRejected = rejectedLocally || persistedRejected;

  return (
    <article
      className={`rounded-md border p-4 transition-colors ${
        isAccepted
          ? "border-success/40 bg-success/5"
          : isRejected
            ? "border-error/40 bg-error/5 opacity-70"
            : "border-border bg-background"
      }`}
    >
      <header className="flex items-start justify-between gap-3 mb-2 flex-wrap">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={badge.variant}>{badge.label}</Badge>
          <span className="text-[10px] font-mono text-muted-foreground uppercase tracking-wide">
            {itemKindLabel(delta.item_kind)}
          </span>
          <span className="text-[10px] font-mono text-muted-foreground">
            {delta.item_id}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {isAccepted && (
            <Badge variant="outline" className="text-success border-success/40">
              <Check className="h-3 w-3 mr-1" /> accepted
            </Badge>
          )}
          {isRejected && (
            <Badge variant="outline" className="text-error border-error/40">
              <X className="h-3 w-3 mr-1" /> rejected
            </Badge>
          )}
        </div>
      </header>

      {/* Headline: the agent's suggestion in one sentence. */}
      <p className="text-sm font-medium leading-snug">{delta.summary}</p>

      {/* The structured proposed value, rendered explicitly so the
          "currently it sits on … / I suggest …" comparison is obvious.
          Three rendering modes, in priority order:
            1. value unchanged (prior === proposed)
                 → "Rationale updated — value unchanged at X"
            2. new (prior == null && proposed has value)
                 → "New target — X"
            3. modified (prior !== proposed)
                 → "suggested X / before Y" (the original two-line view)
          Mode 1 fixes Bug 1: the synthesizer sometimes emits a delta with
          change_kind="modified" but identical before/after — the rationale
          changed, not the value. Showing "suggested 45% before 45%" was
          misleading; this banner is the correct label. */}
      {(propValue || propLabel || priorValue) && (
        <div className="mt-3 rounded-md bg-muted/30 px-3 py-2">
          {propLabel && (
            <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
              {propLabel}
            </div>
          )}
          {valueUnchanged ? (
            <div className="mt-0.5 text-sm">
              <span className="text-[10px] font-mono uppercase mr-1 text-muted-foreground">
                rationale updated
              </span>
              <span className="text-muted-foreground">— value unchanged at </span>
              <span className="font-mono font-semibold">{propValue}</span>
            </div>
          ) : isNewTarget ? (
            <div className="mt-0.5 text-sm">
              <span className="text-[10px] font-mono uppercase mr-1 text-muted-foreground">
                new {delta.item_kind.replace("_", " ")}
              </span>
              <span className="text-muted-foreground">— </span>
              <span className="font-mono font-semibold">{propValue}</span>
            </div>
          ) : (
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 mt-0.5 text-sm">
              {propValue && (
                <span>
                  <span className="text-[10px] font-mono uppercase mr-1 text-muted-foreground">
                    suggested
                  </span>
                  <span className="font-mono font-semibold">{propValue}</span>
                </span>
              )}
              {priorValue && (
                <span>
                  <span className="text-[10px] font-mono uppercase mr-1 text-muted-foreground">
                    before
                  </span>
                  <span className="font-mono">{priorValue}</span>
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {/* Rationale always visible (was collapsible) so the user doesn't
          have to expand 10 cards to read the reasoning. The synthesizer
          writes 1-2 sentence rationales; they're short.

          Bug 2: "Blocker #N" / "Objection #N" tokens are surfaced as
          clickable chips that reveal the matching prior-round FM
          objection in a popover. When no prior objection matches the
          number, the chip still renders but says "no matching prior
          objection found" — never crashes. */}
      {delta.rationale && (
        <div className="mt-3">
          <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1">
            Reasoning
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            {rationaleSegments.map((seg, i) => {
              if (seg.kind === "text") {
                return <span key={i}>{seg.text}</span>;
              }
              const n = seg.refNumber;
              const obj = typeof n === "number" ? priorObj[n - 1] : undefined;
              const isOpen = openRefIdx === i;
              const tooltipText = obj
                ? `${obj.severity} — ${obj.topic}: ${obj.detail}`
                : `${seg.text} (no matching prior objection found)`;
              return (
                <span key={i} className="relative inline-block align-baseline">
                  <button
                    type="button"
                    onClick={() =>
                      setOpenRefIdx((curr) => (curr === i ? null : i))
                    }
                    onBlur={() =>
                      setOpenRefIdx((curr) => (curr === i ? null : curr))
                    }
                    title={tooltipText}
                    className={
                      "rounded-md px-1.5 py-0.5 mx-0.5 font-mono text-[11px] transition-colors " +
                      (obj
                        ? "bg-warning/10 hover:bg-warning/20 text-warning border border-warning/40"
                        : "bg-muted/40 hover:bg-muted/60 text-muted-foreground border border-border")
                    }
                    aria-expanded={isOpen}
                    aria-label={
                      obj
                        ? `${seg.text} — view prior objection`
                        : `${seg.text} — no matching prior objection`
                    }
                  >
                    {seg.text}
                  </button>
                  {isOpen && (
                    <span
                      role="tooltip"
                      className="absolute z-20 left-0 top-full mt-1 w-80 max-w-[24rem] rounded-md border border-border bg-background p-3 text-xs shadow-md text-left"
                    >
                      {obj ? (
                        <>
                          <div className="flex items-center gap-2 mb-1">
                            <Badge
                              variant={
                                obj.severity === "RED"
                                  ? "error"
                                  : obj.severity === "AMBER"
                                  ? "warning"
                                  : "outline"
                              }
                              className="text-[10px]"
                            >
                              {obj.severity}
                            </Badge>
                            <span className="font-mono text-[10px] uppercase tracking-wide text-muted-foreground">
                              prior {seg.text}
                            </span>
                          </div>
                          <div className="text-foreground font-medium mb-1">
                            {obj.topic}
                          </div>
                          <div className="text-muted-foreground leading-relaxed">
                            {obj.detail}
                          </div>
                        </>
                      ) : (
                        <div className="text-muted-foreground">
                          <span className="font-mono">{seg.text}</span> —
                          no matching prior objection found.
                        </div>
                      )}
                    </span>
                  )}
                </span>
              );
            })}
          </p>
        </div>
      )}

      {/* Live target-progress strip. Renders only for TARGET cards, just
          below the rationale. Three modes:
            * full strip when targetProgress has a numeric current_value
            * "(live state pending: synthesis required)" muted line when
              compute_source signals the data isn't computable
            * not rendered at all for non-target deltas (theme / action) */}
      {delta.item_kind === "target" &&
        (targetProgress ? (
          <LiveProgressStrip progress={targetProgress} />
        ) : (
          <div className="mt-3 rounded-md bg-muted/20 border border-border/40 px-3 py-1.5 text-[11px] text-muted-foreground italic">
            (live state pending: synthesis required)
          </div>
        ))}

      {labels.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
            sources
          </span>
          {labels.map((label) => (
            <button
              key={label}
              type="button"
              onClick={() => onSourceClick?.(label)}
              className="rounded-full bg-accent/30 hover:bg-accent/60 transition-colors px-2 py-0.5 text-[10px] font-mono"
              title="Open the agent's full reasoning"
            >
              {label}
            </button>
          ))}
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={toggleHistory}
          title="Show how this item has evolved across plan iterations"
        >
          <History className="h-3 w-3 mr-1" />
          {history === "loading"
            ? "Loading…"
            : Array.isArray(history)
              ? "Hide history"
              : "History"}
        </Button>
      </div>

      {Array.isArray(history) && history.length > 0 && (
        <div className="mt-3 rounded-md border border-border/40 bg-muted/20 p-3">
          <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-2">
            Item lineage ({history.length} versions)
          </div>
          <ul className="text-xs space-y-2">
            {history.map((h, i) => {
              const valueStr =
                h.value !== null
                  ? `${h.value}${h.unit ? " " + h.unit : ""}`
                  : "—";
              return (
                <li
                  key={`${h.plan_version_id}-${i}`}
                  className="flex items-baseline gap-2 border-l-2 border-border/40 pl-2"
                >
                  <span className="font-mono text-muted-foreground text-[10px] min-w-[64px]">
                    plan #{h.plan_version_id}
                  </span>
                  <Badge variant="outline" className="text-[10px]">
                    {h.role}
                  </Badge>
                  <span className="font-mono">{valueStr}</span>
                  {h.label && (
                    <span className="text-muted-foreground truncate">
                      — {h.label}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {Array.isArray(history) && history.length === 0 && (
        <p className="mt-3 text-xs text-muted-foreground">
          No prior versions for this item — first appearance.
        </p>
      )}
      {history === "error" && (
        <p className="mt-3 text-xs text-error">
          Couldn&apos;t load history; the endpoint may not be available yet.
        </p>
      )}

      {pushbackLines.length > 0 && (
        <div className="mt-3 rounded-md border border-warning/40 bg-warning/5 px-3 py-2">
          <div className="text-[10px] font-mono uppercase tracking-wide text-warning mb-1">
            Your pushback ({pushbackLines.length})
          </div>
          <ul className="text-xs space-y-1">
            {pushbackLines.map((line, i) => (
              <li key={i} className="text-muted-foreground">
                · {line}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* T4.3 — slim re-debate progress / verdict surface. Shown when
          the user clicked Push back and the backend kicked off a slim
          bull/bear/facilitator run scoped to this delta. The
          decision_run_id links into /decisions for the full trail. */}
      {pushbackRun && (
        <div
          className={
            "mt-3 rounded-md px-3 py-2 border flex items-center justify-between gap-3 " +
            (pushbackRun.status === "running"
              ? "border-info/40 bg-info/5"
              : pushbackRun.status === "failed"
              ? "border-error/40 bg-error/5"
              : "border-success/40 bg-success/5")
          }
        >
          <div className="text-xs">
            <span className="font-mono uppercase tracking-wide mr-2">
              {pushbackRun.status === "running"
                ? "Re-debate running…"
                : pushbackRun.status === "failed"
                ? "Re-debate failed"
                : "Re-debate complete"}
            </span>
            <span className="text-muted-foreground">
              bull / bear / facilitator scoped to this delta
            </span>
          </div>
          <a
            href={`/decisions/${pushbackRun.decisionRunId}`}
            className="text-xs text-primary hover:underline whitespace-nowrap"
          >
            View trail #{pushbackRun.decisionRunId}
          </a>
        </div>
      )}

      {!isAccepted && !isRejected && (
        <div className="mt-3 flex flex-wrap justify-end gap-2">
          {onPushBack && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onPushBack(delta)}
              disabled={disabled}
              title="Tell the fleet why this isn't right; they re-evaluate with your pushback"
            >
              <MessageSquareWarning className="h-3.5 w-3.5 mr-1" /> Push back
            </Button>
          )}
          {onReject && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setRejectedLocally(true);
                onReject(delta);
              }}
              disabled={disabled}
            >
              <X className="h-3.5 w-3.5 mr-1" /> Reject
            </Button>
          )}
          {onAccept && (
            <Button
              size="sm"
              onClick={() => onAccept(delta)}
              disabled={disabled}
            >
              <Check className="h-3.5 w-3.5 mr-1" /> Accept
            </Button>
          )}
        </div>
      )}
    </article>
  );
}

// One-line strip that sits below the rationale on TARGET cards. Renders:
//   LIVE: <last_observation> · gap <±X> <emoji> <headline>
// Tone is driven by progressStripStyle (success / warning / error / muted).
function LiveProgressStrip({ progress }: { progress: TargetProgress }) {
  const { tone, glyph, headline } = progressStripStyle(progress);
  const tonal = {
    success: "border-success/40 bg-success/5 text-success",
    warning: "border-warning/40 bg-warning/5 text-warning",
    error: "border-error/40 bg-error/5 text-error",
    muted: "border-border/40 bg-muted/20 text-muted-foreground",
  }[tone];
  const gapStr = formatGap(progress.gap_value, progress.target_unit);
  const currentStr = formatLiveValue(progress.current_value, progress.current_unit);
  const noLive = progress.current_value === null;
  return (
    <div
      className={`mt-3 rounded-md border px-3 py-1.5 text-[11px] flex flex-wrap items-center gap-x-3 gap-y-1 ${tonal}`}
      title={progress.last_observation}
    >
      <span className="font-mono uppercase tracking-wide text-[9px] opacity-70">
        live
      </span>
      {noLive ? (
        <span className="text-muted-foreground italic">
          {progress.last_observation || "(not yet computable)"}
        </span>
      ) : (
        <>
          <span className="font-mono">{currentStr}</span>
          {progress.gap_value !== null && (
            <span className="font-mono opacity-80">gap {gapStr}</span>
          )}
          <span className="font-mono text-[12px] leading-none">{glyph}</span>
          <span className="opacity-90">{headline}</span>
        </>
      )}
    </div>
  );
}
