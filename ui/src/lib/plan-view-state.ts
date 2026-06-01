/**
 * Wave 8 Piece A — /plan page state discriminator.
 *
 * The page renders one of five layouts driven by a single derived
 * ``view_state``. Centralising the rule here (instead of leaving it
 * implicit in JSX conditionals) makes the matrix explicit + testable —
 * one branch per test in ``__tests__/plan-view-state.test.ts``.
 *
 * Precedence rules (top wins):
 *   1. ``pending_draft_triage`` — a real draft is pending. Beats
 *      everything per open-question Q3 default in the wave-8 plan
 *      doc: when both a pending draft AND a current plan exist, the
 *      user triggered the new cycle and wants to act on it.
 *   2. ``in_flight_synthesis`` — synthesis is running and there is no
 *      pending draft to surface. The in-flight card is the primary
 *      content.
 *   3. ``recap_current`` — a canonical current plan exists with no
 *      pending draft + no in-flight run. This is the post-Accept-All
 *      state the wave-8 layout fills out (charts + headline + etc.).
 *      A stale draft surfacing from ``/api/plan/draft`` is ignored
 *      here — the recap is the authoritative view when a current
 *      plan exists.
 *   4. ``stale_fallback_with_warning`` — only a superseded draft
 *      exists (no current plan, no pending draft, no in-flight). Show
 *      the existing fallback view with its warning banner; this is
 *      the only branch where today's stale UX is the right answer.
 *   5. ``no_plan`` — nothing exists yet. User needs to ingest a plan
 *      or kick off a synthesis.
 */

export type PlanViewState =
  | "no_plan"
  | "pending_draft_triage"
  | "in_flight_synthesis"
  | "recap_current"
  | "stale_fallback_with_warning";

/**
 * Minimal shapes used by the discriminator. The page passes the full
 * DTOs in; only the fields below influence the branch decision, so we
 * narrow the input here to keep the unit tests free of unrelated
 * fixture fields.
 */
export interface PlanViewStateInputs {
  plan: { plan_version_id: number | null } | null;
  draft: { effective_role?: string | null } | null;
  inFlightSynthesis: { decision_run_id: number } | null;
}

/**
 * A draft row counts as "pending" when its ``effective_role`` is
 * either missing/null (legacy rows) or the literal "draft". Any other
 * value (``fm_rejected``, ``superseded``, ``current``, …) means the
 * backend fell back to an old row that should never drive the primary
 * triage UX.
 */
function isPendingDraft(
  draft: PlanViewStateInputs["draft"],
): boolean {
  if (draft == null) return false;
  const role = draft.effective_role;
  return role == null || role === "" || role === "draft";
}

function isStaleDraft(draft: PlanViewStateInputs["draft"]): boolean {
  if (draft == null) return false;
  const role = draft.effective_role;
  return role != null && role !== "" && role !== "draft";
}

export function derivePlanViewState(
  inputs: PlanViewStateInputs,
): PlanViewState {
  const { plan, draft, inFlightSynthesis } = inputs;

  if (isPendingDraft(draft)) return "pending_draft_triage";
  if (inFlightSynthesis != null) return "in_flight_synthesis";
  if (plan?.plan_version_id != null) return "recap_current";
  if (isStaleDraft(draft)) return "stale_fallback_with_warning";
  return "no_plan";
}
