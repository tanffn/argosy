/**
 * Wave 8 Piece A — state-discriminator matrix for the /plan page.
 *
 * The five branches map to the layout the page renders. One test per
 * branch pins the discriminator so a future state addition forces an
 * explicit update here. This is the regression net that catches the
 * #61/#62 fall-through — where the page silently dropped to a stale
 * draft view because the rules in the JSX expression were implicit.
 */

import { describe, expect, it } from "vitest";

import {
  derivePlanViewState,
  type PlanViewStateInputs,
} from "../plan-view-state";

// Tiny factory so each test can express only the fields that matter for
// its branch and inherit "nothing else exists" defaults from the base.
function inputs(overrides: Partial<PlanViewStateInputs> = {}): PlanViewStateInputs {
  return {
    plan: null,
    draft: null,
    inFlightSynthesis: null,
    ...overrides,
  };
}

describe("derivePlanViewState — five-branch discriminator", () => {
  it("returns no_plan when nothing exists", () => {
    expect(derivePlanViewState(inputs())).toBe("no_plan");
  });

  it("returns pending_draft_triage when a real draft is pending", () => {
    expect(
      derivePlanViewState(
        inputs({ draft: { effective_role: "draft" } }),
      ),
    ).toBe("pending_draft_triage");
  });

  it("treats a draft with undefined effective_role as pending (legacy rows)", () => {
    // Older DraftResponse rows omit effective_role; the existing UI
    // treats absent as "this is a draft". Pin the same semantics.
    expect(derivePlanViewState(inputs({ draft: {} }))).toBe(
      "pending_draft_triage",
    );
  });

  it("returns in_flight_synthesis when a run is going AND no pending draft", () => {
    expect(
      derivePlanViewState(
        inputs({ inFlightSynthesis: { decision_run_id: 99 } }),
      ),
    ).toBe("in_flight_synthesis");
  });

  it("returns recap_current when a current plan exists AND no pending draft AND no in-flight", () => {
    expect(
      derivePlanViewState(
        inputs({ plan: { plan_version_id: 19 } }),
      ),
    ).toBe("recap_current");
  });

  it("returns stale_fallback_with_warning when only a superseded draft exists", () => {
    // No current plan, no pending draft, no in-flight — but /api/plan/draft
    // fell back to an old fm_rejected row. This is the only branch where
    // the existing stale-banner UX is the right answer.
    expect(
      derivePlanViewState(
        inputs({
          draft: { effective_role: "fm_rejected" },
        }),
      ),
    ).toBe("stale_fallback_with_warning");
  });
});

describe("derivePlanViewState — precedence", () => {
  it("pending_draft_triage wins over recap_current when both apply (Q3 default)", () => {
    // Spec Q3: user accepted plan A → current_plan set, then triggered a
    // new synthesis that produced pending draft B. /plan shows draft B's
    // triage flow; the recap is reachable via a header link.
    expect(
      derivePlanViewState(
        inputs({
          plan: { plan_version_id: 19 },
          draft: { effective_role: "draft" },
        }),
      ),
    ).toBe("pending_draft_triage");
  });

  it("pending_draft_triage wins over in_flight_synthesis when both apply", () => {
    // A pending draft from a prior run + a new synthesis kicked off but
    // not yet emitting its draft. Triage of the existing draft remains
    // the primary surface; the in-flight banner rides on top.
    expect(
      derivePlanViewState(
        inputs({
          draft: { effective_role: "draft" },
          inFlightSynthesis: { decision_run_id: 99 },
        }),
      ),
    ).toBe("pending_draft_triage");
  });

  it("recap_current ignores a stale draft when a current plan exists", () => {
    // This is the post-Accept-All regression #62 hit: /api/plan/draft
    // fell back to plan_version=18 (fm_rejected) while plan_version=19
    // was canonical. Without this rule the page renders the stale
    // banner; with it the recap takes over.
    expect(
      derivePlanViewState(
        inputs({
          plan: { plan_version_id: 19 },
          draft: { effective_role: "fm_rejected" },
        }),
      ),
    ).toBe("recap_current");
  });

  it("recap_current ignores a stale draft with the 'superseded' role too", () => {
    // Stale-role variant: the backend's fallback can return any of
    // {fm_rejected, superseded, current, …}. Pin that the recap rule
    // applies for "superseded" as well, not just "fm_rejected".
    expect(
      derivePlanViewState(
        inputs({
          plan: { plan_version_id: 19 },
          draft: { effective_role: "superseded" },
        }),
      ),
    ).toBe("recap_current");
  });

  it("in_flight_synthesis wins over recap_current when no pending draft", () => {
    // User clicked Run synthesis on a page already showing recap. While
    // the run is going, surface the in-flight card; the recap returns
    // when the new draft lands (or when synthesis completes without one).
    expect(
      derivePlanViewState(
        inputs({
          plan: { plan_version_id: 19 },
          inFlightSynthesis: { decision_run_id: 99 },
        }),
      ),
    ).toBe("in_flight_synthesis");
  });

  it("in_flight_synthesis wins over stale_fallback_with_warning", () => {
    expect(
      derivePlanViewState(
        inputs({
          draft: { effective_role: "fm_rejected" },
          inFlightSynthesis: { decision_run_id: 99 },
        }),
      ),
    ).toBe("in_flight_synthesis");
  });
});
