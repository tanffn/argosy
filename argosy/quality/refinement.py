"""Read-only refinement orchestration for the living-plan decision core.

This module is the single entry point that DECIDES what machinery is needed
to apply a proposed set of ChangeRequests to the living plan. It never mutates
anything, never calls an LLM, never touches the DB or network — it is a pure
deterministic classifier that coordinates the blast-radius sizer and the plan
invariant kernel.

WHY A SEPARATE ORCHESTRATION LAYER:
  The blast-radius sizer (blast_radius.py) models what the DERIVATION GRAPH
  thinks will change. The plan invariant kernel (plan_risk_kernel.py) models what
  the PLAN DOCUMENT must satisfy. These are complementary, not redundant:

    * The sizer catches structural and policy changes the graph can reason about
      (plan-identity axis, owner-domain changes, cross-owner edges, etc.).

    * The kernel catches plan-level numeric invariants the graph cannot yet see
      (allocation_sum ≠ 100%, single-name cap breach) because the plan doc is not
      yet modelled inside the derivation graph.

  The honest gap is documented in blast_radius.py: ``invalidates_global_invariant``
  is INERT when the caller passes neither pre_doc nor post_doc, because the sizer
  cannot reconstruct the post-doc from the graph alone. This module closes that
  gap at the orchestration level: if ``post_doc`` is supplied, we run
  ``evaluate_plan_invariants`` explicitly and FORCE FULL_REBUILD on any violation.

WHY THE INVARIANT OVERRIDE IS MANDATORY:
  The classifier is a heuristic over graph-structure signals; it cannot predict
  every plan-level invariant break. An incoherent post-state (allocation sums to
  110%, single-name cap breached, etc.) MUST NOT land as a SCOPED_EDIT or
  BOUNDED_REDERIVE — those tiers assume the post-state is semantically valid.
  The invariant net is therefore the final safety gate: if the post-doc fails, we
  escalate to FULL_REBUILD unconditionally, regardless of what the classifier said.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from argosy.quality.blast_radius import (
    BlastRadius,
    ChangeRequest,
    Tier,
    TierConfig,
    classify,
    size_blast_radius,
)
from argosy.quality.plan_risk_kernel import InvariantReport, evaluate_plan_invariants


__all__ = [
    "RefinementDecision",
    "run_refinement",
    "summary",
]


@dataclass(frozen=True)
class RefinementDecision:
    """Immutable result of run_refinement().

    Fields
    ------
    tier:
        The effort tier required to safely apply the requested changes.
        One of Tier.SCOPED_EDIT, BOUNDED_REDERIVE, FULL_REBUILD.
    reason:
        Human-readable string identifying which signal determined the tier.
        When forced_by_invariant=True, this describes the invariant breach rather
        than the blast-radius classifier signal.
    blast_radius:
        The full BlastRadius computed by size_blast_radius().  Retained for
        audit / logging — callers should not re-derive it.
    invariant_report:
        The InvariantReport from evaluate_plan_invariants(post_doc), or None
        when post_doc was not supplied.  Retained for audit.
    forced_by_invariant:
        True when the invariant net overrode the blast-radius classifier's tier
        and forced an escalation to FULL_REBUILD.  False in all other cases,
        including when the classifier itself returned FULL_REBUILD.
    """
    tier: Tier
    reason: str
    blast_radius: BlastRadius
    invariant_report: InvariantReport | None
    forced_by_invariant: bool


def run_refinement(
    graph,
    change_requests: Sequence[ChangeRequest],
    *,
    pre_doc=None,
    post_doc=None,
    cfg: TierConfig | None = None,
) -> RefinementDecision:
    """Decide the refinement tier for a proposed set of ChangeRequests.

    This function is the decision core: it coordinates the blast-radius sizer
    and the plan invariant kernel and returns a frozen RefinementDecision. It
    does NOT mutate the graph, the doc, or any shared state.

    Steps
    -----
    1. Size the blast radius (sandbox clone of the graph — original untouched).
    2. Classify the blast radius into a Tier via the heuristic classifier.
    3. Money-safety net: if post_doc is supplied, run evaluate_plan_invariants.
       If it has ANY violation, FORCE the tier to FULL_REBUILD regardless of the
       classifier's verdict. This is the mandatory invariant override — an
       incoherent post-state must never land as a scoped or bounded edit.
    4. Return a frozen RefinementDecision carrying all intermediate results.

    Args:
        graph:           The live derivation graph (read-only after entry).
        change_requests: Sequence of proposed ChangeRequests to evaluate.
        pre_doc:         Optional pre-change plan document. Passed through to
                         size_blast_radius for the graph-level invariant field
                         (see blast_radius.py module docstring for the known gap).
        post_doc:        Optional post-change plan document. When supplied, the
                         invariant net runs against it (step 3). When absent, the
                         net is inert (invariant_report=None, forced_by_invariant=False).
        cfg:             Optional TierConfig for classifier thresholds. Defaults to
                         TierConfig() (standard thresholds).

    Returns:
        A frozen RefinementDecision.

    Raises:
        ValueError: propagated from size_blast_radius when a ChangeRequest targets
                    a non-INPUT (DERIVED/SURFACE) node.
    """
    effective_cfg = cfg if cfg is not None else TierConfig()

    # Step 1 — size blast radius on a sandbox clone
    br = size_blast_radius(graph, change_requests, pre_doc=pre_doc, post_doc=post_doc)

    # Step 2 — classify blast radius
    tier, reason = classify(br, cfg=effective_cfg)

    # Step 3 — money-safety net: run invariants on the post-doc if supplied
    invariant_report: InvariantReport | None = None
    forced_by_invariant = False

    if post_doc is not None:
        invariant_report = evaluate_plan_invariants(post_doc)
        if not invariant_report.ok:
            # Force FULL_REBUILD regardless of classifier tier.  This is the
            # safety override: an incoherent post-state must never land as T0/T1.
            codes = ", ".join(v.code for v in invariant_report.violations)
            reason = (
                f"plan invariant breach forces full rebuild (violations: {codes}); "
                f"original classifier reason: {reason}"
            )
            tier = Tier.FULL_REBUILD
            forced_by_invariant = True

    return RefinementDecision(
        tier=tier,
        reason=reason,
        blast_radius=br,
        invariant_report=invariant_report,
        forced_by_invariant=forced_by_invariant,
    )


def summary(decision: RefinementDecision) -> str:
    """Render a one-line human-readable summary of a RefinementDecision.

    Format:
        "<tier>: <reason> | dirtied=<n> owners=<owner1,owner2,...> [invariant-forced]"

    The ``[invariant-forced]`` suffix is appended only when forced_by_invariant=True.
    Intended for functional harness logs and CI output.
    """
    br = decision.blast_radius
    n_dirtied = len(br.dirtied_keys)
    owners_str = ",".join(sorted(br.owner_domains)) if br.owner_domains else "(none)"
    line = (
        f"{decision.tier.value}: {decision.reason} "
        f"| dirtied={n_dirtied} owners={owners_str}"
    )
    if decision.forced_by_invariant:
        line += " [invariant-forced]"
    return line
