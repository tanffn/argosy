"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  MessageCircle,
  Minus,
  RefreshCw,
  Sparkles,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  api,
  type FMObjection,
  type FMObjectionStance,
  type FMObjectionStateRow,
  type FMObjectionTranslation,
} from "@/lib/api";

interface FMObjectionsCardProps {
  objections: FMObjection[];
  userId: string;
  // When provided, the user-state toggle (agree / defer / disagree) is
  // rendered per objection and the "Start new round with my decisions"
  // CTA is enabled. Without it the card falls back to the legacy
  // re-synthesize-all-concerns flow only.
  planVersionId?: number | null;
  // When provided, renders a primary CTA below the list that re-synthesizes
  // the plan with the Fund Manager's objections fed back to the fleet as
  // guidance. The caller wires this to /api/advisor/check-in with the
  // formatted objection text.
  onResynthesize?: () => void | Promise<void>;
  resynthesizing?: boolean;
  // T4.7 — when provided, the "Discuss" button per objection opens a
  // conversation with the advisor seeded with the objection content.
  onDiscussObjection?: (o: FMObjection) => void;
  // Called after start-new-round succeeds so the caller can flip its
  // "synthesis running" state on without re-calling the API to learn
  // the new decision_audit_token.
  onStartNewRound?: (
    decisionAuditToken: string,
    decisionRunId: number,
  ) => void;
}

function severityClasses(s: FMObjection["severity"]) {
  switch (s) {
    case "RED":
      return {
        badge: "error" as const,
        dot: "bg-error",
        ring: "border-error/40 bg-error/5",
      };
    case "AMBER":
      return {
        badge: "secondary" as const,
        dot: "bg-warning",
        ring: "border-warning/40 bg-warning/5",
      };
    case "YELLOW":
    default:
      return {
        badge: "outline" as const,
        dot: "bg-muted-foreground",
        ring: "border-border/60 bg-muted/20",
      };
  }
}

// Per-objection display state.
//
//   - undefined  → no on-demand fetch attempted yet. If the objection
//     already carries a precomputed ``translation``, the card shows it
//     by default and offers the inverse toggle ("Show original Fund
//     Manager wording"); otherwise the card shows the original detail
//     and offers the "Explain in plain English" button (lazy fallback).
//   - "loading"  → on-demand fetch in flight (only happens when the
//     precomputed translation is missing).
//   - "error"    → on-demand fetch failed.
//   - FMObjectionTranslation → lazy fetch succeeded.
//
// Separately we track a boolean ``showOriginal`` per row so the user
// can toggle back to the FM's original wording even when a translation
// is being displayed. This is the *instant* toggle the precomputed
// cache makes possible.
type RowFetchState = FMObjectionTranslation | "loading" | "error" | undefined;

export function FMObjectionsCard(props: FMObjectionsCardProps) {
  const {
    objections,
    userId,
    planVersionId,
    onResynthesize,
    resynthesizing,
    onDiscussObjection,
    onStartNewRound,
  } = props;

  // Lazy-fetch cache for objections whose precomputed translation is
  // missing (translator failed at draft-load time). Keyed by index in
  // the sorted list.
  const [lazyFetches, setLazyFetches] = useState<Record<number, RowFetchState>>(
    {},
  );
  // "Show original Fund Manager wording" toggle per row. Default false
  // (when a translation is available we surface the plain-English
  // version first; the user clicks to drop back to raw FM text).
  const [showOriginal, setShowOriginal] = useState<Record<number, boolean>>({});

  // Per-objection user stance (AGREE / DISAGREE / DEFER). Default is
  // DEFER. Persisted server-side via PUT /api/plan/draft/objections/
  // state so the toggle survives page navigation.
  const [stances, setStances] = useState<
    Record<number, FMObjectionStateRow>
  >({});
  const [counterDrafts, setCounterDrafts] = useState<Record<number, string>>(
    {},
  );
  const [stanceError, setStanceError] = useState<string | null>(null);
  const [startingNewRound, setStartingNewRound] = useState(false);
  const [startNewRoundError, setStartNewRoundError] = useState<string | null>(
    null,
  );

  // Hydrate from server on mount / when planVersionId changes so the
  // toggle is rendered in its persisted state after a page navigation.
  useEffect(() => {
    if (planVersionId == null) return;
    let cancelled = false;
    api
      .planDraftObjectionStateGet(userId, planVersionId)
      .then((r) => {
        if (cancelled) return;
        const next: Record<number, FMObjectionStateRow> = {};
        const drafts: Record<number, string> = {};
        for (const [k, v] of Object.entries(r.states)) {
          const idx = Number(k);
          if (!Number.isFinite(idx)) continue;
          next[idx] = v;
          if (v.counter_position) drafts[idx] = v.counter_position;
        }
        setStances(next);
        setCounterDrafts(drafts);
      })
      .catch(() => {
        // Non-fatal — the toggle just starts in the all-DEFER default.
      });
    return () => {
      cancelled = true;
    };
  }, [userId, planVersionId]);

  if (objections.length === 0) return null;

  const lazyTranslate = async (idx: number, o: FMObjection) => {
    setLazyFetches((prev) => ({ ...prev, [idx]: "loading" }));
    try {
      const t = await api.planDraftObjectionTranslate(userId, {
        topic: o.topic,
        detail: o.detail,
        severity: o.severity,
      });
      setLazyFetches((prev) => ({
        ...prev,
        [idx]: {
          headline: t.headline,
          plain_english: t.plain_english,
          recommended_actions: t.recommended_actions,
        },
      }));
    } catch {
      setLazyFetches((prev) => ({ ...prev, [idx]: "error" }));
    }
  };

  // Sort RED → AMBER → YELLOW so the most-critical concerns sit on top.
  const sevOrder: Record<FMObjection["severity"], number> = {
    RED: 0,
    AMBER: 1,
    YELLOW: 2,
  };
  const sorted = [...objections].sort(
    (a, b) => sevOrder[a.severity] - sevOrder[b.severity],
  );

  const persistStance = async (
    idx: number,
    stance: FMObjectionStance,
    counter: string | null,
    o: FMObjection,
  ) => {
    if (planVersionId == null) return;
    setStanceError(null);
    try {
      await api.planDraftObjectionStatePut({
        user_id: userId,
        plan_version_id: planVersionId,
        objection_index: idx,
        stance,
        counter_position: counter,
        topic: o.topic,
        detail: o.detail,
      });
      setStances((prev) => ({
        ...prev,
        [idx]: { stance, counter_position: counter ?? null },
      }));
    } catch (e: unknown) {
      setStanceError(e instanceof Error ? e.message : String(e));
    }
  };

  const onPickStance = (
    idx: number,
    next: FMObjectionStance,
    o: FMObjection,
  ) => {
    if (next === "DISAGREE") {
      // Optimistically flip the toggle so the textarea reveals; only
      // persist once the user has supplied a counter-position (the
      // backend rejects empty counter for DISAGREE with HTTP 400).
      const draft = (counterDrafts[idx] ?? "").trim();
      setStances((prev) => ({
        ...prev,
        [idx]: { stance: "DISAGREE", counter_position: draft || null },
      }));
      if (draft) {
        void persistStance(idx, "DISAGREE", draft, o);
      }
      return;
    }
    void persistStance(idx, next, null, o);
  };

  const onBlurCounter = (idx: number, o: FMObjection) => {
    const cur = stances[idx];
    if (!cur || cur.stance !== "DISAGREE") return;
    const draft = (counterDrafts[idx] ?? "").trim();
    if (!draft) return;
    if (draft === (cur.counter_position ?? "")) return; // no-op
    void persistStance(idx, "DISAGREE", draft, o);
  };

  const startNewRound = async () => {
    if (planVersionId == null) return;
    setStartNewRoundError(null);
    setStartingNewRound(true);
    try {
      const r = await api.planDraftObjectionsStartNewRound(
        userId,
        planVersionId,
      );
      if (onStartNewRound) {
        onStartNewRound(r.decision_audit_token, r.decision_run_id);
      }
    } catch (e: unknown) {
      setStartNewRoundError(e instanceof Error ? e.message : String(e));
    } finally {
      setStartingNewRound(false);
    }
  };

  // Buckets for the summary line + the "Start new round" CTA enable check.
  // Indices are positions in the SORTED list (same as the backend's).
  let nAgreed = 0;
  let nDisagreed = 0;
  let nDeferred = 0;
  for (let i = 0; i < sorted.length; i++) {
    const s = stances[i]?.stance ?? "DEFER";
    if (s === "AGREE") nAgreed++;
    else if (s === "DISAGREE") nDisagreed++;
    else nDeferred++;
  }
  const canStartNewRound =
    planVersionId != null && (nAgreed > 0 || nDisagreed > 0);

  return (
    <div className="rounded-md border border-error/40 bg-error/5 p-4">
      <div className="flex items-center gap-2 mb-3">
        <AlertTriangle className="h-4 w-4 text-error" />
        <h3 className="text-sm font-semibold tracking-wide uppercase text-error">
          Fund Manager objections ({objections.length})
        </h3>
        <span className="ml-2 text-[10px] font-mono text-muted-foreground">
          (the agent that signs off on the synthesized plan)
        </span>
      </div>
      <ul className="flex flex-col gap-2">
        {sorted.map((o, i) => {
          const cls = severityClasses(o.severity);

          // Pick the active translation: precomputed wins, else the
          // lazy fetch (when the user clicked "Explain in plain
          // English" because the precomputed one was missing).
          const precomputed: FMObjectionTranslation | null =
            o.translation ?? null;
          const lazyState = lazyFetches[i];
          const lazyTranslated: FMObjectionTranslation | null =
            lazyState && lazyState !== "loading" && lazyState !== "error"
              ? lazyState
              : null;
          const activeTranslation: FMObjectionTranslation | null =
            precomputed ?? lazyTranslated;

          // When precomputed translation exists, default to showing
          // plain English; the user can flip to original via the
          // toggle. When only a lazy translation exists, same logic.
          // When no translation exists, original is the only option.
          const rowShowsOriginal = showOriginal[i] === true;
          const renderTranslated =
            activeTranslation !== null && !rowShowsOriginal;

          return (
            <li
              key={i}
              className={`rounded-md border ${cls.ring} p-3 text-sm`}
            >
              <div className="flex items-start gap-2">
                <span
                  className={`mt-1.5 h-2 w-2 rounded-full ${cls.dot} flex-shrink-0`}
                  aria-hidden
                />
                <div className="flex-1">
                  <div className="flex items-center justify-between gap-2 mb-1 flex-wrap">
                    <span className="font-medium">
                      {renderTranslated && activeTranslation
                        ? activeTranslation.headline
                        : o.topic}
                    </span>
                    <Badge variant={cls.badge}>{o.severity}</Badge>
                  </div>
                  {renderTranslated && activeTranslation ? (
                    <>
                      <p className="text-sm whitespace-pre-line">
                        {activeTranslation.plain_english}
                      </p>
                      {activeTranslation.recommended_actions.length > 0 && (
                        <div className="mt-2">
                          <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1">
                            Recommended actions
                          </div>
                          <ul className="text-xs list-disc list-inside space-y-0.5">
                            {activeTranslation.recommended_actions.map(
                              (a, j) => (
                                <li key={j}>{a}</li>
                              ),
                            )}
                          </ul>
                        </div>
                      )}
                    </>
                  ) : (
                    <p className="text-sm text-muted-foreground whitespace-pre-line">
                      {o.detail}
                    </p>
                  )}
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    {activeTranslation ? (
                      // Precomputed (or freshly lazy-fetched) translation
                      // is available — render the instant toggle. No
                      // spinner; the data is already in hand.
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        onClick={() =>
                          setShowOriginal((prev) => ({
                            ...prev,
                            [i]: !rowShowsOriginal,
                          }))
                        }
                      >
                        <Sparkles className="h-3 w-3 mr-1" />
                        {rowShowsOriginal
                          ? "Explain in plain English"
                          : "Show original Fund Manager wording"}
                      </Button>
                    ) : (
                      // No precomputed translation — fall back to the
                      // on-demand POST. This path runs only when the
                      // server-side cache helper returned nothing for
                      // this slot (translator failed at draft-load).
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        onClick={() => lazyTranslate(i, o)}
                        disabled={lazyState === "loading"}
                      >
                        <Sparkles className="h-3 w-3 mr-1" />
                        {lazyState === "loading"
                          ? "Translating…"
                          : "Explain in plain English"}
                      </Button>
                    )}
                    {lazyState === "error" && (
                      <span className="text-xs text-error">
                        Translation failed; raw text shown above.
                      </span>
                    )}
                    {onDiscussObjection && (
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        onClick={() => onDiscussObjection(o)}
                      >
                        <MessageCircle className="h-3 w-3 mr-1" />
                        Discuss with advisor
                      </Button>
                    )}
                  </div>

                  {/* Per-objection stance toggle (AGREE / DEFER / DISAGREE).
                      Only rendered when a planVersionId is wired so we know
                      where to PUT. */}
                  {planVersionId != null && (() => {
                    const curStance: FMObjectionStance =
                      stances[i]?.stance ?? "DEFER";
                    const counter = counterDrafts[i] ?? "";
                    return (
                      <div className="mt-3 pt-2 border-t border-border/30">
                        <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1.5">
                          Your stance on this objection
                        </div>
                        <div className="flex flex-wrap items-center gap-1">
                          <Button
                            size="sm"
                            variant={
                              curStance === "AGREE" ? "default" : "outline"
                            }
                            className="h-7 text-xs"
                            onClick={() => onPickStance(i, "AGREE", o)}
                            aria-pressed={curStance === "AGREE"}
                          >
                            <Check className="h-3 w-3 mr-1" />
                            Agree
                          </Button>
                          <Button
                            size="sm"
                            variant={
                              curStance === "DEFER" ? "default" : "outline"
                            }
                            className="h-7 text-xs"
                            onClick={() => onPickStance(i, "DEFER", o)}
                            aria-pressed={curStance === "DEFER"}
                          >
                            <Minus className="h-3 w-3 mr-1" />
                            Defer
                          </Button>
                          <Button
                            size="sm"
                            variant={
                              curStance === "DISAGREE" ? "default" : "outline"
                            }
                            className="h-7 text-xs"
                            onClick={() => onPickStance(i, "DISAGREE", o)}
                            aria-pressed={curStance === "DISAGREE"}
                          >
                            <X className="h-3 w-3 mr-1" />
                            Disagree
                          </Button>
                        </div>
                        {curStance === "DISAGREE" && (
                          <div className="mt-2">
                            <label
                              htmlFor={`counter-${i}`}
                              className="block text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1"
                            >
                              Your counter-position (the new round treats this
                              as authoritative)
                            </label>
                            <textarea
                              id={`counter-${i}`}
                              className="w-full text-sm rounded-md border border-border/60 bg-background px-2 py-1.5 min-h-[3.5rem] focus:outline-none focus:ring-1 focus:ring-ring"
                              value={counter}
                              onChange={(e) =>
                                setCounterDrafts((prev) => ({
                                  ...prev,
                                  [i]: e.target.value,
                                }))
                              }
                              onBlur={() => onBlurCounter(i, o)}
                              placeholder="e.g. I prefer a 12% drawdown trigger over the 8% the FM proposed."
                            />
                            <p className="text-[10px] text-muted-foreground mt-1">
                              {counter.trim()
                                ? "Saves on blur."
                                : "Required for disagree — the new round needs a position to honor."}
                            </p>
                          </div>
                        )}
                      </div>
                    );
                  })()}
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      {(onResynthesize || planVersionId != null) && (
        <div className="mt-3 pt-3 border-t border-error/30 flex flex-col gap-2">
          {planVersionId != null && (
            <div className="flex items-center justify-between gap-3 flex-wrap text-xs">
              <span className="font-mono text-muted-foreground">
                <span className="text-foreground font-semibold">{nAgreed}</span>{" "}
                agreed ·{" "}
                <span className="text-foreground font-semibold">
                  {nDisagreed}
                </span>{" "}
                disagreed ·{" "}
                <span className="text-foreground font-semibold">
                  {nDeferred}
                </span>{" "}
                deferred
              </span>
            </div>
          )}
          {stanceError && (
            <p className="text-xs text-error" role="alert">
              Couldn&apos;t save your stance: {stanceError}
            </p>
          )}
          {startNewRoundError && (
            <p className="text-xs text-error" role="alert">
              Couldn&apos;t start new round: {startNewRoundError}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            Don&apos;t want to handle these yourself? Either re-run the fleet
            with every objection as guidance, or mark each one above and start
            a new round driven by your decisions.
          </p>
          <div className="flex items-center justify-end gap-2 flex-wrap">
            {onResynthesize && (
              <Button
                onClick={onResynthesize}
                disabled={resynthesizing || startingNewRound}
                variant="outline"
                size="sm"
                className="whitespace-nowrap"
              >
                <RefreshCw
                  className={`h-3.5 w-3.5 mr-1 ${
                    resynthesizing ? "animate-spin" : ""
                  }`}
                />
                {resynthesizing
                  ? "Re-synthesizing…"
                  : "Re-synthesize with all concerns"}
              </Button>
            )}
            {planVersionId != null && (
              <Button
                onClick={startNewRound}
                disabled={
                  !canStartNewRound || startingNewRound || resynthesizing
                }
                variant="default"
                size="sm"
                className="whitespace-nowrap"
                title={
                  canStartNewRound
                    ? "Compose guidance from your AGREE/DISAGREE decisions and start a new synthesis round"
                    : "Mark at least one objection AGREE or DISAGREE first"
                }
              >
                <RefreshCw
                  className={`h-3.5 w-3.5 mr-1 ${
                    startingNewRound ? "animate-spin" : ""
                  }`}
                />
                {startingNewRound
                  ? "Starting new round…"
                  : "Start new round with my decisions"}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
