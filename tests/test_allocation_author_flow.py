"""The author→verify→bounce orchestration — the control flow of the inversion.

The fleet authors; determinism verifies and, on a fixable failure, bounces the
machine-readable failures back to the SAME author (once or twice); if it still can't
pass, or the author is unavailable, the caller falls back to the deterministic engine
(explicitly labelled degraded). Injectable author/verify so it's fully testable with
no live LLM."""

from __future__ import annotations

from argosy.services.allocation_author.flow import run_allocation_author
from argosy.services.allocation_author.proposal import (
    AllocationProposal,
    AuthoredOrderIntent,
    Buy,
)
from argosy.services.allocation_author.verifier import (
    GateFailure,
    GateReport,
    GateStatus,
)

_PACKET = {
    "deployable_usd": 180_000.0,
    "holdings": {"SCHD": 264_000.0},
    "known_symbols": {"EXUS", "SPMV", "FWRA"},
}


def _intent() -> AuthoredOrderIntent:
    return AuthoredOrderIntent(
        thesis="Fill the governing sleeve gap.",
        thesis_type="diversifier",
        falsifier="The instrument no longer supplies the intended exposure.",
        catalyst_description="Next scheduled portfolio review",
        catalyst_date="2027-01-31",
        expectation="The allocation gap closes.",
        expectation_due_date="2027-02-28",
        success_measure="sleeve moves toward target without a risk breach",
        expected_upside_multiple=2,
    )


def _good():
    return AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[
            Buy(
                symbol="EXUS",
                amount_usd=120_000.0,
                sleeve="ex-US",
                claimed_us_weight=0.0,
                order_intent=_intent(),
            ),
            Buy(
                symbol="SPMV",
                amount_usd=60_000.0,
                sleeve="US low-vol",
                claimed_us_weight=1.0,
                order_intent=_intent(),
            ),
        ],
        rationale="fill the ex-US and low-vol gaps; no US-large-cap into a NVDA-heavy book",
    )


def _bad_fwra():
    return AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[
            Buy(
                symbol="FWRA",
                amount_usd=180_000.0,
                sleeve="ex-US",
                claimed_us_weight=0.0,
                order_intent=_intent(),
            )
        ],
    )


def test_accepts_a_clean_first_proposal():
    out = run_allocation_author(_PACKET, author_fn=lambda p, fb: _good())
    assert out.status == "accepted"
    assert out.attempts == 1


def test_bounces_then_accepts_on_revision():
    calls = {"n": 0}

    def author(packet, feedback):
        calls["n"] += 1
        if calls["n"] == 1:
            assert feedback is None
            return _bad_fwra()  # treats FWRA as ex-US → REVISION
        assert feedback and any("FWRA" in f.detail for f in feedback)  # got the reason
        return _good()

    out = run_allocation_author(_PACKET, author_fn=author, max_revisions=2)
    assert out.status == "accepted"
    assert out.attempts == 2


def test_gives_up_after_max_revisions():
    out = run_allocation_author(_PACKET, author_fn=lambda p, fb: _bad_fwra(), max_revisions=2)
    assert out.status == "rejected"
    assert out.attempts == 3  # initial + 2 revisions
    assert out.report is not None and out.report.failures


def test_revision_feedback_accumulates_across_the_run():
    feedback_seen = []
    verify_calls = {"n": 0}

    def author(packet, feedback):
        feedback_seen.append(list(feedback or []))
        return _good()

    def verify(proposal, packet):
        verify_calls["n"] += 1
        if verify_calls["n"] == 1:
            return GateReport(
                status=GateStatus.REVISION_REQUIRED,
                failures=[GateFailure("judgment", "do not buy IWQU", "revision")],
            )
        if verify_calls["n"] == 2:
            return GateReport(
                status=GateStatus.REVISION_REQUIRED,
                failures=[GateFailure("arithmetic", "fix conservation", "revision")],
            )
        return GateReport(status=GateStatus.ACCEPT)

    out = run_allocation_author(
        _PACKET,
        author_fn=author,
        verify=verify,
        max_revisions=2,
    )
    assert out.status == "accepted"
    assert [failure.code for failure in feedback_seen[2]] == [
        "judgment",
        "arithmetic",
    ]


def test_block_is_terminal_and_does_not_burn_revision_budget():
    calls = {"author": 0, "verify": 0}

    def author(packet, feedback):
        calls["author"] += 1
        return _good()

    def verify(proposal, packet):
        calls["verify"] += 1
        return GateReport(
            status=GateStatus.BLOCK,
            failures=[
                GateFailure(
                    "team_disagreement_unresolved",
                    "The team still disagrees after re-review.",
                    "block",
                )
            ],
        )

    out = run_allocation_author(
        _PACKET,
        author_fn=author,
        verify=verify,
        max_revisions=6,
    )

    assert out.status == "rejected"
    assert out.attempts == 1
    assert calls == {"author": 1, "verify": 1}
    assert out.report is not None
    assert out.report.failures[0].code == "team_disagreement_unresolved"


def test_author_unavailable_signals_fallback():
    def boom(packet, feedback):
        raise RuntimeError("claude.exe timeout")

    out = run_allocation_author(_PACKET, author_fn=boom)
    assert out.status == "unavailable"
    assert out.proposal is None


def test_author_returning_none_is_unavailable():
    out = run_allocation_author(_PACKET, author_fn=lambda p, fb: None)
    assert out.status == "unavailable"
