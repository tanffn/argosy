"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  Flag,
  Loader2,
  MessageCircle,
  Minus,
  RefreshCw,
  Sparkles,
  Users,
  X,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  api,
  type FMObjection,
  type FMObjectionAnalystStance,
  type FMObjectionDialogueResolution,
  type FMObjectionDialogueRow,
  type FMObjectionStance,
  type FMObjectionStateRow,
  type FMObjectionTranslation,
} from "@/lib/api";
import { useWSEvents } from "@/lib/ws";

// FM-objection ZigZag (T4.9) — canonical analyst class → role mapping.
// Mirrors argosy/agents/analyst_responder.py::ANALYST_AGENT_NAME_TO_ROLE
// so the UI parses the same agent_report:... references the backend
// recognises. Keeping the table inline (no shared module) is deliberate:
// the dialogue feature is intentionally localized to this card.
const ANALYST_AGENT_NAME_TO_ROLE: Record<string, string> = {
  ConcentrationAnalystAgent: "concentration",
  TechnicalAnalystAgent: "technical",
  FundamentalsAnalystAgent: "fundamentals",
  NewsAnalystAgent: "news",
  SentimentAnalystAgent: "sentiment",
  MacroAnalystAgent: "macro",
  FxAnalystAgent: "fx",
  TaxAnalystAgent: "tax",
  HouseholdBudgetAnalystAgent: "household_budget",
  PlanCritiqueAgent: "plan_critique",
};

const ANALYST_ROLE_TO_DISPLAY: Record<string, string> = {
  concentration: "Concentration",
  technical: "Technical",
  fundamentals: "Fundamentals",
  news: "News",
  sentiment: "Sentiment",
  macro: "Macro",
  fx: "FX",
  tax: "Tax",
  household_budget: "Household budget",
  plan_critique: "Plan critique",
};

// Pulls every distinct agent_report:<AgentClassName> reference from an
// objection's detail text, then maps each to the canonical analyst role.
// Returns the list in encounter order, deduplicated. Empty array when
// no recognized analyst refs are present (button is disabled in that
// case — the FM's concern is structural / no specific analyst owner).
function parseAnalystRefsFromObjection(detail: string): string[] {
  if (!detail) return [];
  const re = /agent_report:([A-Z][A-Za-z]+Agent)/g;
  const seen = new Set<string>();
  const out: string[] = [];
  for (const m of detail.matchAll(re)) {
    const name = m[1];
    const role = ANALYST_AGENT_NAME_TO_ROLE[name];
    if (role && !seen.has(role)) {
      seen.add(role);
      out.push(role);
    }
  }
  return out;
}

interface FMObjectionsCardProps {
  objections: FMObjection[];
  userId: string;
  // When set to "carried_over" or "not_evaluated", a banner explains the
  // verdict-provenance state at the top of the card. Default
  // "evaluated" (real FM verdict for this draft).
  verdictStatus?: "evaluated" | "carried_over" | "not_evaluated";
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
  // `objectionNumber` is the 1-based human-facing identifier rendered
  // on the card ("FM-Obj #N"); the parent uses it to tag the seed so
  // the resulting chat / decisions trail can be cross-referenced.
  onDiscussObjection?: (o: FMObjection, objectionNumber: number) => void;
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

// Per-stance badge palette for the analyst's response. CONCEDE is
// green-on-success, REBUT is red-on-error, CLARIFY is amber. Mirrors
// the resolution palette below so the user can scan the whole
// dialogue at a glance.
function stanceBadgeClasses(s: FMObjectionAnalystStance) {
  switch (s) {
    case "CONCEDE":
      return "border-success/40 bg-success/10 text-success";
    case "REBUT":
      return "border-error/40 bg-error/10 text-error";
    case "CLARIFY":
    default:
      return "border-warning/40 bg-warning/10 text-warning";
  }
}

// Per-resolution badge palette for the FM's final verdict. Green for
// FM_ACCEPTS_ANALYST (the FM was convinced), neutral for
// FM_MAINTAINS_OBJECTION (no change), amber for FM_REVISES_OBJECTION
// (third-reading), red for ESCALATE_TO_USER (genuine impasse).
function resolutionBadgeClasses(r: FMObjectionDialogueResolution) {
  switch (r) {
    case "FM_ACCEPTS_ANALYST":
      return "border-success/40 bg-success/10 text-success";
    case "FM_REVISES_OBJECTION":
      return "border-warning/40 bg-warning/10 text-warning";
    case "ESCALATE_TO_USER":
      return "border-error/40 bg-error/10 text-error";
    case "FM_MAINTAINS_OBJECTION":
    default:
      return "border-border/60 bg-muted/30 text-muted-foreground";
  }
}

const RESOLUTION_DISPLAY: Record<FMObjectionDialogueResolution, string> = {
  FM_ACCEPTS_ANALYST: "FM accepts analyst",
  FM_MAINTAINS_OBJECTION: "FM maintains objection",
  FM_REVISES_OBJECTION: "FM revises objection",
  ESCALATE_TO_USER: "Escalate to user",
};

/**
 * Renders the analyst's response + FM's verdict from one prior
 * dialogue. While the dialogue is in flight (status="starting" |
 * "running") shows a spinner card instead. When status="error" shows
 * the error message inline. Empty when no dialogue exists yet (status
 * == "idle" AND dialogue == null).
 *
 * Extracted as a sibling component (not inlined) so the FMObjectionsCard
 * render path stays readable and the spinner / outcome / error states
 * are exhaustively covered in one place.
 */
function DialogueResultPanel(props: {
  status: "idle" | "starting" | "running" | "error";
  errorMessage: string | null;
  dialogue: FMObjectionDialogueRow | null;
}) {
  const { status, errorMessage, dialogue } = props;

  if (status === "starting" || status === "running") {
    return (
      <div className="mt-3 pt-2 border-t border-border/30">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          <span>
            Dialogue in progress… the analyst is drafting a response and the
            Fund Manager is preparing a final verdict. Usually 30-60 seconds.
          </span>
        </div>
      </div>
    );
  }

  if (status === "error" && errorMessage) {
    return (
      <div className="mt-3 pt-2 border-t border-border/30">
        <p className="text-xs text-error" role="alert">
          Couldn&apos;t kick off the dialogue: {errorMessage}
        </p>
      </div>
    );
  }

  if (!dialogue) return null;
  if (dialogue.status !== "completed") {
    // Row exists but didn't complete (failed / superseded). Show the
    // raw status so the user knows something happened but the verdict
    // isn't trustworthy.
    return (
      <div className="mt-3 pt-2 border-t border-border/30">
        <p className="text-xs text-muted-foreground">
          Prior dialogue ended with status{" "}
          <span className="font-mono">{dialogue.status}</span>.
        </p>
      </div>
    );
  }

  return (
    <div className="mt-3 pt-3 border-t border-border/30 flex flex-col gap-3">
      {/* Analyst response section. */}
      {dialogue.analyst_stance && (
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Badge
              variant="outline"
              className={stanceBadgeClasses(dialogue.analyst_stance)}
            >
              {dialogue.analyst_stance}
            </Badge>
            <span className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
              {ANALYST_ROLE_TO_DISPLAY[dialogue.analyst_role] ??
                dialogue.analyst_role}{" "}
              analyst responded
            </span>
          </div>
          {dialogue.analyst_reasoning_md && (
            <p className="text-xs whitespace-pre-line text-muted-foreground">
              {dialogue.analyst_reasoning_md}
            </p>
          )}
          {dialogue.analyst_suggested_fix && (
            <div className="mt-1.5 rounded-sm border border-border/40 bg-muted/30 p-2">
              <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-0.5">
                Analyst suggested fix
              </div>
              <p className="text-xs whitespace-pre-line">
                {dialogue.analyst_suggested_fix}
              </p>
            </div>
          )}
        </div>
      )}

      {/* FM verdict section. */}
      {dialogue.resolution && (
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Badge
              variant="outline"
              className={resolutionBadgeClasses(dialogue.resolution)}
            >
              {dialogue.resolution === "FM_ACCEPTS_ANALYST" && (
                <Check className="h-3 w-3 mr-1" />
              )}
              {dialogue.resolution === "ESCALATE_TO_USER" && (
                <Flag className="h-3 w-3 mr-1" />
              )}
              {RESOLUTION_DISPLAY[dialogue.resolution]}
            </Badge>
            <span className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
              Fund Manager verdict
            </span>
          </div>
          {dialogue.fm_reasoning_md && (
            <p className="text-xs whitespace-pre-line text-muted-foreground">
              {dialogue.fm_reasoning_md}
            </p>
          )}
          {dialogue.resolution === "FM_REVISES_OBJECTION" &&
            dialogue.updated_objection_text && (
              <div className="mt-1.5 rounded-sm border border-warning/40 bg-warning/5 p-2">
                <div className="text-[10px] font-mono uppercase tracking-wide text-warning mb-0.5">
                  Revised objection text (for next round)
                </div>
                <p className="text-xs whitespace-pre-line">
                  {dialogue.updated_objection_text}
                </p>
              </div>
            )}
          {dialogue.resolution === "FM_ACCEPTS_ANALYST" &&
            dialogue.suggested_plan_amendment && (
              <div className="mt-1.5 rounded-sm border border-success/40 bg-success/5 p-2 flex flex-col gap-1.5">
                <div>
                  <div className="text-[10px] font-mono uppercase tracking-wide text-success mb-0.5">
                    Plan amendment (FM-accepted)
                  </div>
                  <p className="text-xs whitespace-pre-line">
                    {dialogue.suggested_plan_amendment}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs self-start"
                  onClick={() => {
                    // Stash the amendment text on the clipboard so the
                    // user can paste it as guidance to the next /start-
                    // new-round call. A future iteration can wire this
                    // directly into a "queue for next round" buffer —
                    // for now clipboard is the lowest-friction surface.
                    void navigator.clipboard?.writeText(
                      dialogue.suggested_plan_amendment ?? "",
                    );
                  }}
                  title="Copy the amendment text to the clipboard so you can paste it into the next round's guidance."
                >
                  <Sparkles className="h-3 w-3 mr-1" />
                  Copy amendment for next round
                </Button>
              </div>
            )}
        </div>
      )}
    </div>
  );
}

export function FMObjectionsCard(props: FMObjectionsCardProps) {
  const {
    objections,
    userId,
    verdictStatus = "evaluated",
    planVersionId,
    onResynthesize,
    resynthesizing,
    onDiscussObjection,
    onStartNewRound,
  } = props;
  const isCarriedOver = verdictStatus === "carried_over";

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

  // FM-objection ZigZag (T4.9) per-objection state.
  //
  //   * ``dialogueSelectedRole`` — when the objection mentions multiple
  //     analysts, the user picks one from the dropdown before clicking
  //     "Discuss with X". For single-analyst objections this is null
  //     and the click goes straight to the analyst named in the text.
  //   * ``dialogueRuns`` — the most recent dialogue for each objection
  //     index. Hydrated from GET .../dialogues on mount and updated as
  //     the user kicks off new dialogues. Latest dialogue wins (the
  //     backend orders by started_at DESC).
  //   * ``dialogueStatus`` — transient UI state machine. "idle" = no
  //     dialogue running; "starting" = POST in flight; "running" =
  //     waiting for the WS completion event; "error" = dispatch failed.
  //   * ``dialogueErrors`` — last per-objection error message; null
  //     when the prior dispatch succeeded.
  const [dialogueSelectedRole, setDialogueSelectedRole] = useState<
    Record<number, string>
  >({});
  const [dialogueRuns, setDialogueRuns] = useState<
    Record<number, FMObjectionDialogueRow | null>
  >({});
  const [dialogueStatus, setDialogueStatus] = useState<
    Record<number, "idle" | "starting" | "running" | "error">
  >({});
  const [dialogueErrors, setDialogueErrors] = useState<
    Record<number, string | null>
  >({});

  // Hydrate the most-recent dialogue per objection so the user sees
  // prior results after a page reload. We fetch lazily once per mount
  // for the indices in the current sorted list. Best-effort — any
  // failure leaves the dialogue UI in its empty-state.
  // The fetch fires only when planVersionId is known (the GET requires
  // a pending draft to disambiguate the objection index).
  useEffect(() => {
    if (planVersionId == null) return;
    const indices = objections.map((_, i) => i);
    let cancelled = false;
    (async () => {
      const results = await Promise.allSettled(
        indices.map((i) =>
          api.planDraftObjectionDialogues(i, userId).then((r) => ({ i, r })),
        ),
      );
      if (cancelled) return;
      const next: Record<number, FMObjectionDialogueRow | null> = {};
      for (const res of results) {
        if (res.status !== "fulfilled") continue;
        const latest = res.value.r.dialogues[0] ?? null;
        next[res.value.i] = latest;
      }
      setDialogueRuns((prev) => ({ ...prev, ...next }));
    })();
    return () => {
      cancelled = true;
    };
    // We intentionally key on planVersionId + objections.length: a
    // re-mount on a different draft or a different objection count
    // should re-hydrate. We do NOT depend on objections itself because
    // the parent passes a fresh array on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, planVersionId, objections.length]);

  // Subscribe to dialogue completion events. When the backend emits
  // ``plan.fm_objection.dialogue.completed`` for one of our objections,
  // we flip the status from "running" → "idle" and refetch the
  // dialogue row so the structured outcome renders.
  useWSEvents<{
    user_id?: string;
    plan_version_id?: number;
    objection_index?: number;
    decision_run_id?: number;
    analyst_role?: string;
    resolution?: FMObjectionDialogueResolution;
    error?: string | null;
  }>(["plan.fm_objection.dialogue.completed"], {
    onEvent: (e) => {
      if (e.payload.user_id !== userId) return;
      if (planVersionId != null && e.payload.plan_version_id !== planVersionId) {
        return;
      }
      const idx = e.payload.objection_index;
      if (typeof idx !== "number") return;
      setDialogueStatus((prev) => ({
        ...prev,
        [idx]: e.payload.error ? "error" : "idle",
      }));
      if (e.payload.error) {
        setDialogueErrors((prev) => ({
          ...prev,
          [idx]: e.payload.error ?? "dialogue failed",
        }));
      }
      // Refetch the latest dialogue row so the outcome renders.
      void api
        .planDraftObjectionDialogues(idx, userId)
        .then((r) =>
          setDialogueRuns((prev) => ({
            ...prev,
            [idx]: r.dialogues[0] ?? null,
          })),
        )
        .catch(() => {});
    },
  });

  // Kick off one dialogue. Validates the analyst role, calls POST
  // .../discuss, and flips the per-objection status to "running" so
  // the spinner shows. The WS event flips it back when complete; if
  // the POST itself fails we land in "error".
  const kickOffDialogue = useCallback(
    async (idx: number, o: FMObjection, role: string) => {
      setDialogueErrors((prev) => ({ ...prev, [idx]: null }));
      setDialogueStatus((prev) => ({ ...prev, [idx]: "starting" }));
      try {
        const resp = await api.planDraftObjectionDiscuss(idx, {
          user_id: userId,
          analyst_role: role,
        });
        if (resp.status === "cost_cap_refused") {
          setDialogueStatus((prev) => ({ ...prev, [idx]: "error" }));
          setDialogueErrors((prev) => ({
            ...prev,
            [idx]:
              resp.detail ??
              "cost cap reached — try again after the 24h window rolls",
          }));
          return;
        }
        setDialogueStatus((prev) => ({ ...prev, [idx]: "running" }));
        // No-op for ``o`` — the parent already has the objection text;
        // we just needed the param to assert the caller is intentional
        // about which objection it's discussing.
        void o;
      } catch (e: unknown) {
        setDialogueStatus((prev) => ({ ...prev, [idx]: "error" }));
        setDialogueErrors((prev) => ({
          ...prev,
          [idx]: e instanceof Error ? e.message : String(e),
        }));
      }
    },
    [userId],
  );

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
    <div
      className={`rounded-md border p-4 ${
        isCarriedOver ? "border-warning/40 bg-warning/5" : "border-error/40 bg-error/5"
      }`}
    >
      <div className="flex items-center gap-2 mb-3">
        <AlertTriangle
          className={`h-4 w-4 ${isCarriedOver ? "text-warning" : "text-error"}`}
        />
        <h3
          className={`text-sm font-semibold tracking-wide uppercase ${
            isCarriedOver ? "text-warning" : "text-error"
          }`}
        >
          {isCarriedOver
            ? `Carried-over objections (${objections.length})`
            : `Fund Manager objections (${objections.length})`}
        </h3>
        <span className="ml-2 text-[10px] font-mono text-muted-foreground">
          {isCarriedOver
            ? "from a prior draft — not re-evaluated against current state"
            : "(the agent that signs off on the synthesized plan)"}
        </span>
      </div>
      {isCarriedOver && (
        <div className="mb-3 rounded-md border border-warning/40 bg-warning/10 p-2.5 text-xs">
          The Fund Manager hasn&apos;t scored this draft. The amendment
          flow that produced it writes a synthetic phase record but
          doesn&apos;t invoke the FM agent. The objections below are
          inherited from the most recent earlier draft that had a real FM
          verdict — they may not all still apply. Press{" "}
          <strong>Run synthesis</strong> at the top of the page for a
          fresh FM verdict against the current state.
        </div>
      )}
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
              id={`fm-obj-${i + 1}`}
              className={`rounded-md border ${cls.ring} p-3 text-sm scroll-mt-20`}
            >
              <div className="flex items-start gap-2">
                <span
                  className={`mt-1.5 h-2 w-2 rounded-full ${cls.dot} flex-shrink-0`}
                  aria-hidden
                />
                <div className="flex-1">
                  <div className="flex items-center justify-between gap-2 mb-1 flex-wrap">
                    <div className="flex items-center gap-2 min-w-0">
                      <Badge
                        variant="outline"
                        className="font-mono text-[10px] tracking-wide shrink-0"
                        title={
                          "Stable identifier for this objection on the current draft. " +
                          "Reference as “FM-Obj #" + (i + 1) + "” in chat / notes."
                        }
                      >
                        FM-OBJ #{i + 1}
                      </Badge>
                      {o.carried_over && (
                        <Badge
                          variant="outline"
                          className="border-warning/40 bg-warning/10 text-warning font-mono text-[10px] shrink-0"
                          title={
                            o.carried_over_from_plan_version_id != null
                              ? `Carried over from draft #${o.carried_over_from_plan_version_id} — not re-evaluated against the current draft's inputs.`
                              : "Carried over from a prior draft — not re-evaluated against the current draft's inputs."
                          }
                        >
                          carried over
                          {o.carried_over_from_plan_version_id != null
                            ? ` (#${o.carried_over_from_plan_version_id})`
                            : ""}
                        </Badge>
                      )}
                      <span className="font-medium">
                        {renderTranslated && activeTranslation
                          ? activeTranslation.headline
                          : o.topic}
                      </span>
                    </div>
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
                        onClick={() => onDiscussObjection(o, i + 1)}
                      >
                        <MessageCircle className="h-3 w-3 mr-1" />
                        Discuss with advisor
                      </Button>
                    )}
                    {/* FM-objection ZigZag — "Discuss with [analyst]" button.
                        Renders disabled when the objection text mentions no
                        recognised agent (the concern is structural / no
                        analyst owner). When multiple analyst refs are
                        present, a select dropdown appears alongside so the
                        user picks which analyst to discuss with. */}
                    {(() => {
                      const analystRoles = parseAnalystRefsFromObjection(
                        o.detail,
                      );
                      const status = dialogueStatus[i] ?? "idle";
                      const inFlight =
                        status === "starting" || status === "running";
                      if (analystRoles.length === 0) {
                        return (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 text-xs"
                            disabled
                            title={
                              "This objection isn't owned by a single analyst — " +
                              "use 'Discuss with advisor' or mark a stance instead."
                            }
                          >
                            <Users className="h-3 w-3 mr-1" />
                            No analyst owner
                          </Button>
                        );
                      }
                      const pickedRole =
                        dialogueSelectedRole[i] ?? analystRoles[0];
                      const buttonLabel = inFlight
                        ? "Dialogue in progress…"
                        : `Discuss with ${
                            ANALYST_ROLE_TO_DISPLAY[pickedRole] ?? pickedRole
                          }`;
                      return (
                        <div className="flex items-center gap-1">
                          {analystRoles.length > 1 && (
                            <select
                              value={pickedRole}
                              onChange={(e) =>
                                setDialogueSelectedRole((prev) => ({
                                  ...prev,
                                  [i]: e.target.value,
                                }))
                              }
                              disabled={inFlight}
                              className="h-7 text-xs rounded-md border border-border/60 bg-background px-1.5"
                              aria-label={`Pick analyst to discuss objection ${i + 1} with`}
                            >
                              {analystRoles.map((r) => (
                                <option key={r} value={r}>
                                  {ANALYST_ROLE_TO_DISPLAY[r] ?? r}
                                </option>
                              ))}
                            </select>
                          )}
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 text-xs"
                            disabled={inFlight}
                            onClick={() =>
                              void kickOffDialogue(i, o, pickedRole)
                            }
                          >
                            {inFlight ? (
                              <Loader2 className="h-3 w-3 mr-1 animate-spin" />
                            ) : (
                              <Users className="h-3 w-3 mr-1" />
                            )}
                            {buttonLabel}
                          </Button>
                        </div>
                      );
                    })()}
                  </div>

                  {/* Dialogue result panel. Renders the most recent dialogue
                      for this objection so the user sees the analyst's
                      stance + FM's verdict; if the analyst proposed a fix
                      AND the FM accepted, surfaces an "Apply this fix" CTA.
                      The block is hidden while no dialogue exists yet, and
                      replaced by a spinner card while the dialogue is in
                      flight (the WS-completion handler replaces the spinner
                      with the structured outcome). */}
                  <DialogueResultPanel
                    status={dialogueStatus[i] ?? "idle"}
                    errorMessage={dialogueErrors[i] ?? null}
                    dialogue={dialogueRuns[i] ?? null}
                  />

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
                        {curStance === "AGREE" && (
                          <div className="mt-2">
                            <label
                              htmlFor={`resolution-${i}`}
                              className="block text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-1"
                            >
                              Resolution note (optional — what did you do?)
                            </label>
                            <textarea
                              id={`resolution-${i}`}
                              className="w-full text-sm rounded-md border border-border/60 bg-background px-2 py-1.5 min-h-[3.5rem] focus:outline-none focus:ring-1 focus:ring-ring"
                              value={counter}
                              onChange={(e) =>
                                setCounterDrafts((prev) => ({
                                  ...prev,
                                  [i]: e.target.value,
                                }))
                              }
                              onBlur={() => {
                                const draft = (
                                  counterDrafts[i] ?? ""
                                ).trim();
                                if (
                                  draft &&
                                  draft !==
                                    (stances[i]?.counter_position ?? "")
                                ) {
                                  void persistStance(i, "AGREE", draft, o);
                                }
                              }}
                              placeholder="e.g. Updated goals_yaml via /advisor to split apartment goal from household contribution; combined liability now 1 M instead of 3 M."
                            />
                            <p className="text-[10px] text-muted-foreground mt-1">
                              Records how this objection was resolved
                              outside the plan loop (chat edit, manual
                              change, etc.). Saves on blur.
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
