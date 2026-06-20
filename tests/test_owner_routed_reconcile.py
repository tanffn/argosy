"""Phase 3c — the owner-routed reconcile ROUND: a reader BLOCK is routed to its
owners, each owner proposes a targeted fix (set_value / prose_fix / decline), and
the prose fixes are spliced into the draft bodies — figure value changes are handed
back as change-requests for the cycle. One bounded round, deterministic (the owner
proposer + the prose editor are injected)."""
from __future__ import annotations

from argosy.orchestrator.flows.plan_synthesis.whole_artifact_reader import (
    CoherenceFinding,
    WholeArtifactVerdict,
)
from argosy.quality.change_adjudication import ChangeKind
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.finding_remediation import RemediationProposal
from argosy.quality.owner_routed_reconcile import run_owner_routed_reconcile_round


def _finding(*, subject_type, kind="contradiction", severity="BLOCKER",
             detail="d", surfaces=()):
    return CoherenceFinding(
        kind=kind, severity=severity, detail=detail,
        surfaces_cited=list(surfaces), subject_type=subject_type,
    )


def _verdict(findings):
    return WholeArtifactVerdict(overall_assessment="BLOCK", findings=findings)


def _graph():
    g = DerivationGraph()
    g.add_node(Node(key="retirement.earliest_safe_age", kind=NodeKind.INPUT, value=46.0))
    return g


class _FixedProposer:
    """Owner proposer that returns one fixed proposal for every objection."""

    def __init__(self, proposal):
        self._p = proposal
        self.calls = 0

    def propose(self, **kw):
        self.calls += 1
        return self._p


# A deterministic prose editor: replaces any flagged snippet with a number-free,
# safe corrected clause (passes surgical_reconcile's anti-fabrication guard).
def _stub_editor(_prompt):
    return "reconciled clause"


def test_owner_prose_fix_is_spliced_into_the_body():
    body = "We retire at 46 with funds running through 48."
    verdict = _verdict([
        _finding(subject_type="retirement_age_headline",
                 detail="age 46 vs withdrawal-through-48 drift",
                 surfaces=["We retire at 46 with funds running through 48."]),
    ])
    proposer = _FixedProposer(RemediationProposal(
        kind="prose_fix", instruction="age 46 is correct; align the narrative"))
    res = run_owner_routed_reconcile_round(
        reader_verdict=verdict,
        bodies={"long": body, "medium": "", "short": ""},
        graph=_graph(), proposer=proposer, editor=_stub_editor,
    )
    assert proposer.calls == 1  # the figure objection reached its owner
    assert res.made_progress
    assert len(res.prose_edits) == 1
    assert "reconciled clause" in res.bodies["long"]
    assert not res.value_change_requests
    assert not res.unowned


def test_owner_set_value_is_surfaced_not_applied_as_a_body_edit():
    # A figure change is surfaced for the caller's deeper-correction (full re-synth)
    # fallback — it does NOT mutate the body the reader reads, so it is not progress
    # here. Applying it without re-rendering every surface would recreate a cross-
    # surface contradiction.
    body = "We retire at 46."
    verdict = _verdict([
        _finding(subject_type="retirement_age_headline",
                 surfaces=["We retire at 46."]),
    ])
    proposer = _FixedProposer(RemediationProposal(
        kind="set_value", value=48.0, rationale="bridge funds spend to 48"))
    res = run_owner_routed_reconcile_round(
        reader_verdict=verdict,
        bodies={"long": body, "medium": "", "short": ""},
        graph=_graph(), proposer=proposer, editor=_stub_editor,
    )
    assert len(res.value_change_requests) == 1
    cr = res.value_change_requests[0]
    assert cr.kind is ChangeKind.SET_INPUT
    assert cr.target_node_key == "retirement.earliest_safe_age"
    assert cr.payload["value"] == 48.0
    assert not res.prose_edits           # a figure change is NOT a body edit
    assert res.bodies["long"] == body    # body untouched
    assert not res.made_progress         # → caller falls through to the fallback


def test_owner_decline_is_recorded_and_makes_no_progress():
    body = "FI margin is -167,735 NIS; the plan is honestly short."
    verdict = _verdict([
        _finding(subject_type="fi_capital_sufficiency", surfaces=[body]),
    ])
    proposer = _FixedProposer(RemediationProposal(
        kind="decline", rationale="the margin is correctly short"))
    res = run_owner_routed_reconcile_round(
        reader_verdict=verdict,
        bodies={"long": body, "medium": "", "short": ""},
        graph=_graph(), proposer=proposer, editor=_stub_editor,
    )
    assert len(res.declines) == 1
    assert "correctly short" in res.declines[0]["rationale"]
    assert not res.prose_edits and not res.value_change_requests
    assert not res.made_progress
    assert res.bodies["long"] == body  # untouched


def test_prose_routed_subject_is_edited_without_calling_the_proposer():
    # sgln_ucits_membership has an owner (INVESTMENT) but no single figure node —
    # the owner fixes the PROSE directly; no figure objection, no proposer call.
    snippet = "SGLN sits in the UCITS basket alongside the equity sleeve."
    body = f"Strategy note: {snippet}"
    verdict = _verdict([
        _finding(subject_type="sgln_ucits_membership",
                 detail="SGLN is gold, not equity — membership claim is wrong",
                 surfaces=[snippet]),
    ])
    proposer = _FixedProposer(RemediationProposal(kind="decline"))
    res = run_owner_routed_reconcile_round(
        reader_verdict=verdict,
        bodies={"long": body, "medium": "", "short": ""},
        graph=_graph(), proposer=proposer, editor=_stub_editor,
    )
    assert proposer.calls == 0  # prose-routed never goes through the figure proposer
    assert len(res.prose_edits) == 1
    assert "reconciled clause" in res.bodies["long"]
    assert res.made_progress


def test_unroutable_with_surfaces_is_caught_by_the_lead_and_edited():
    # An empty subject_type has no owner — the catch-all Lead still prose-fixes it
    # (it cites a surface), so nothing is silently dropped.
    snippet = "The receipts appendix double-counts the cash line."
    body = f"Appendix: {snippet}"
    verdict = _verdict([_finding(subject_type="", surfaces=[snippet])])
    proposer = _FixedProposer(RemediationProposal(kind="decline"))
    res = run_owner_routed_reconcile_round(
        reader_verdict=verdict,
        bodies={"long": body, "medium": "", "short": ""},
        graph=_graph(), proposer=proposer, editor=_stub_editor,
    )
    assert len(res.prose_edits) == 1
    assert "reconciled clause" in res.bodies["long"]
    assert not res.unowned  # it had a surface, so the Lead handled it


def test_two_findings_citing_the_same_surface_are_edited_once():
    # Coalesce: the same surface cited twice must be edited a single time (a second
    # pass over an already-mutated body would silently no-op).
    snippet = "SGLN sits in the equity sleeve."
    body = f"Note: {snippet}"
    verdict = _verdict([
        _finding(subject_type="sgln_ucits_membership", detail="wrong basket",
                 surfaces=[snippet]),
        _finding(subject_type="sgln_ucits_membership", detail="also wrong sleeve",
                 surfaces=[snippet]),
    ])
    proposer = _FixedProposer(RemediationProposal(kind="decline"))
    res = run_owner_routed_reconcile_round(
        reader_verdict=verdict,
        bodies={"long": body, "medium": "", "short": ""},
        graph=_graph(), proposer=proposer, editor=_stub_editor,
    )
    assert len(res.prose_edits) == 1            # one span, one edit
    assert res.bodies["long"].count("reconciled clause") == 1


def test_prose_finding_whose_surface_is_absent_is_reported_unaddressed():
    # The cited surface is not a substring of any body — the editor cannot splice it;
    # it must surface as unaddressed (not silently dropped) and make no progress.
    verdict = _verdict([
        _finding(subject_type="sgln_ucits_membership", detail="d",
                 surfaces=["a surface that is not in the body"]),
    ])
    proposer = _FixedProposer(RemediationProposal(kind="decline"))
    res = run_owner_routed_reconcile_round(
        reader_verdict=verdict,
        bodies={"long": "unrelated body text", "medium": "", "short": ""},
        graph=_graph(), proposer=proposer, editor=_stub_editor,
    )
    assert not res.prose_edits
    assert len(res.unaddressed) == 1
    assert not res.made_progress


def test_unroutable_without_surfaces_remains_genuinely_unowned():
    # No subject AND no cited surface — there is nothing a prose edit can touch;
    # it surfaces as unowned (fail-loud), never silently dropped.
    verdict = _verdict([_finding(subject_type="", surfaces=[], detail="vague")])
    proposer = _FixedProposer(RemediationProposal(kind="decline"))
    res = run_owner_routed_reconcile_round(
        reader_verdict=verdict,
        bodies={"long": "body", "medium": "", "short": ""},
        graph=_graph(), proposer=proposer, editor=_stub_editor,
    )
    assert len(res.unowned) == 1
    assert not res.prose_edits
    assert not res.made_progress
