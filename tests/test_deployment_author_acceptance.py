"""ACCEPTANCE — the case that triggered the pivot.

$180k to deploy, book ~60% NVDA, FWRA in the menu (known ~62% US). A plain LLM prompt
beat Argosy here by reasoning with world-knowledge: FWRA is US-heavy so it's NOT ex-US
diversification, and you don't pile more US onto an already-concentrated book. This
proves the pivot's guardrails enforce those two judgment calls end-to-end:

  1. the verifier CATCHES today's failure (FWRA treated as ex-US);
  2. the author→verify→bounce loop CONVERGES when the author fixes it on revision;
  3. the accepted allocation adds NO mislabeled ex-US and stays on estate-safe UCITS.

(There is deliberately no tax-reserve step — CGT on a sale is paid from that sale, not
pre-funded from unrelated deployment cash.) Author is a stateful fake keyed on the
bounced feedback — no LLM, no subprocess."""
from __future__ import annotations

from argosy.services.allocation_author.packet import build_decision_packet
from argosy.services.allocation_author.proposal import AllocationProposal, Buy
from argosy.services.allocation_author.reliable import CircuitBreaker, authored_allocation
from argosy.services.allocation_author.verifier import (
    GateStatus,
    verify_allocation_proposal,
)


class _Inst:
    def __init__(self, symbol, domicile="IE"):
        self.symbol = symbol
        self.domicile = domicile
        self.weight_within_class_pct = 100.0


class _Cls:
    def __init__(self, label, target_pct, snapshot_category, instruments):
        self.label = label
        self.target_pct = target_pct
        self.snapshot_category = snapshot_category
        self.instruments = instruments


class _Doc:
    nvda_cap_pct = 30.0

    def __init__(self, classes):
        self.classes = classes


def _packet():
    doc = _Doc(classes=[
        _Cls("Ex-US developed", 15.0, "ex_us", [_Inst("EXUS")]),
        _Cls("Global all-world", 10.0, "global", [_Inst("FWRA")]),  # the trap
        _Cls("US low-vol", 20.0, "us_equity", [_Inst("SPMV")]),
    ])
    return build_decision_packet(
        doc=doc,
        holdings_usd={"NVDA": 600_000.0, "SCHD": 264_000.0},
        deployable_usd=180_000.0,
        book_usd=1_000_000.0,
        nvda_lookthrough_usd=600_000.0,
    )


def _bad_fwra_as_exus():
    """Today's failure: pours all $180k into FWRA and calls it ex-US."""
    return AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[Buy(symbol="FWRA", amount_usd=180_000.0, sleeve="ex-US",
                  claimed_us_weight=0.0, justification="international diversification")],
    )


def _good():
    """The judgment-correct move: deploy into a genuine ex-US UCITS + an estate-safe
    US low-vol sleeve — no mislabeled ex-US, no US-heavy all-world on a concentrated
    book."""
    return AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[Buy(symbol="EXUS", amount_usd=130_000.0, sleeve="ex-US developed",
                  claimed_us_weight=0.0, justification="genuine ex-US (MSCI World ex-USA)"),
              Buy(symbol="SPMV", amount_usd=50_000.0, sleeve="US low-vol",
                  claimed_us_weight=1.0, justification="estate-safe UCITS low-vol")],
        rationale="Deployed to genuine ex-US + estate-safe sleeves rather than adding "
                  "US-heavy all-world exposure to an already-concentrated book.",
    )


def test_verifier_catches_todays_failure():
    pkt = _packet()
    report = verify_allocation_proposal(_bad_fwra_as_exus(), pkt)
    assert report.status == GateStatus.REVISION_REQUIRED
    codes = {f.code for f in report.failures}
    # FWRA can't be ex-US.
    assert "lookthrough_claim" in codes


def test_author_bounce_converges_to_the_correct_allocation():
    pkt = _packet()

    def run_author(agent_factory, packet, feedback, *, hard_timeout_s):
        # First pass makes today's mistake; the bounced verifier reason drives the fix.
        if feedback is None:
            return _bad_fwra_as_exus()
        assert any("FWRA" in f.detail for f in feedback)
        return _good()

    out = authored_allocation(
        pkt, user_id="ariel", run_author=run_author,
        breaker=CircuitBreaker(), cache={},
    )
    assert out.status == "accepted"
    assert out.attempts == 2  # bad → bounce → good
    p = out.proposal
    # every dollar accounted for, no tax pre-reserve.
    assert p.cash_to_deploy + p.cash_to_reserve == pkt["deployable_usd"]
    # no FWRA-as-ex-US; genuine ex-US used instead.
    bought = {b.symbol for b in p.buys}
    assert "FWRA" not in bought and "EXUS" in bought


def test_accepted_allocation_passes_a_blind_reverify():
    # The accepted proposal must pass the verifier independently (not just via the loop).
    pkt = _packet()
    report = verify_allocation_proposal(_good(), pkt)
    assert report.status == GateStatus.ACCEPT
    assert report.failures == []
