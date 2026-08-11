"""Phase 2 — stance-revision routing (one voice per position).

A trader that disagrees with a standing SELL/TRIM writes ``PROPOSED STANCE
REVISION:`` + new facts and still emits HOLD. These tests exercise the router
(``argosy/decisions/stance_revision.py``): a revision that clears the blind
FILTER (positive committed-tripwire hit + non-trivial facts + an independent
blind re-derivation that concurs with a keep verdict) is SURFACED for Ariel's
approval (revision_proposed, divergence=True) — but NEVER auto-moves the stance;
reversing a standing SELL on the core is Ariel's PATH decision. Anything else →
revision_rejected. In BOTH cases the stance STAYS the plan SELL/TRIM. The blind
LLM call is stubbed throughout — no live model, isolated tmp SQLite.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.agents.stock_decision import StockDecisionOutput
from argosy.decisions.stance_revision import (
    OUTCOME_PROPOSED,
    OUTCOME_REJECTED,
    parse_stance_revision,
    route_stance_revision,
)
from argosy.services import position_stance as ps
from argosy.services.per_position_thesis import PositionThesis
from argosy.services.position_stance import rebuild_stances
from argosy.services.verdict_registry import write_verdict
from argosy.state.models import Base, HoldingReview, PositionStance, User


def _sell_card(ticker: str = "NVDA") -> PositionThesis:
    return PositionThesis(
        ticker=ticker,
        current_shares=10_000.0,
        current_weight_pct=58.0,
        current_usd_value=4_000_000.0,
        verdict="SELL",
        conviction="HIGH",
        reasoning_md="plan deconcentration pace",
        cited_sources=[],
        target_weight_pct=12.0,
        target_shares=2_000.0,
    )


@pytest.fixture()
def sess(tmp_path, monkeypatch):
    """Isolated SQLite session with a plan projecting a standing SELL on NVDA."""
    engine = create_engine(f"sqlite:///{tmp_path / 'stance_rev.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()

    monkeypatch.setattr(ps, "_load_plan_version", lambda db, uid: SimpleNamespace(id=1, decision_run_id=None))
    monkeypatch.setattr(ps, "_load_portfolio_snapshot", lambda uid, db=None: SimpleNamespace(
        positions=[{"symbol": "NVDA", "shares": 10_000, "usd_value_k": 4_000}], as_of=None,
    ))
    monkeypatch.setattr(ps, "_plan_theses", lambda *a, **k: [_sell_card()])
    return s


def _seed_settled_sell_with_falsifier(s):
    """A settled SELL verdict on NVDA whose falsifier the 'new facts' will hit."""
    write_verdict(
        s,
        user_id="ariel",
        subject="NVDA",
        verdict="SELL",
        conviction="HIGH",
        falsifiers=["china export ban lifted"],
        revisit_triggers=[],
        source_decision_run_id=None,
        reasoning_md="deconcentration",
        settled=True,
    )


def _stance(s, ticker="NVDA") -> PositionStance:
    rows = rebuild_stances(s, "ariel")
    return next(r for r in rows if r.symbol == ticker)


# The label + new facts the trader wrote (Phase 1).
_HIT_RATIONALE = (
    "Holding. PROPOSED STANCE REVISION: China export ban lifted this week and "
    "data-center revenue is re-accelerating — the deconcentration thesis no "
    "longer holds."
)
_MISS_RATIONALE = (
    "Holding. PROPOSED STANCE REVISION: the stock simply looks cheap here and I "
    "have a good feeling about it."
)


def _stub_decide(verdict: str):
    def decide(ticker, *, context, bundle, user_id="ariel"):
        return StockDecisionOutput(
            ticker=ticker, verdict=verdict, confidence="HIGH",
            reason="blind", evidence=[], data_gaps=[],
        )
    return decide


# --------------------------------------------------------------------------- #


def test_parse_label():
    assert parse_stance_revision(None) is None
    assert parse_stance_revision("plain hold, thesis intact") is None
    got = parse_stance_revision(_HIT_RATIONALE)
    assert got and got.lower().startswith("china export ban lifted")


def test_pass_surfaces_but_does_not_move_stance(sess):
    """PASS filter (positive tripwire hit + real facts + blind HOLD) →
    revision_proposed review written, divergence=True, stance STAYS plan SELL."""
    _seed_settled_sell_with_falsifier(sess)
    assert _stance(sess).stance == "SELL"  # baseline

    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE,
        decide=_stub_decide("HOLD"), fetchers={},
    )
    assert res.routed and res.surfaced
    assert res.outcome == OUTCOME_PROPOSED

    hr = sess.query(HoldingReview).filter_by(symbol="NVDA").order_by(HoldingReview.id.desc()).first()
    assert hr.outcome == OUTCOME_PROPOSED and hr.verdict == "HOLD"

    surfaced = _stance(sess)
    assert surfaced.stance == "SELL", "a surfaced revision must NOT auto-move the stance"
    assert surfaced.stance_source == "plan"
    assert surfaced.divergence is True
    assert "approve this revision" in surfaced.reasoning_md.lower()


def test_blind_does_not_concur_rejects_and_keeps_sell(sess):
    """Pushback passes but the independent blind pass ALSO says SELL → rejected."""
    _seed_settled_sell_with_falsifier(sess)
    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE,
        decide=_stub_decide("SELL"), fetchers={},
    )
    assert res.routed and not res.surfaced
    assert res.outcome == OUTCOME_REJECTED

    kept = _stance(sess)
    assert kept.stance == "SELL"
    assert kept.divergence is True


def test_blind_abstain_does_not_concur_rejects(sess):
    """Blind ABSTAIN is not a concurrence → rejected, stance stays SELL."""
    _seed_settled_sell_with_falsifier(sess)
    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE, decide=_stub_decide("ABSTAIN"), fetchers={},
    )
    assert res.routed and not res.surfaced and res.outcome == OUTCOME_REJECTED
    assert _stance(sess).stance == "SELL"


def test_pushback_gate_fails_short_circuits(sess):
    """New facts don't hit any falsifier/trigger → DEFENDED → rejected, no blind call."""
    _seed_settled_sell_with_falsifier(sess)

    called = {"n": 0}

    def decide(*a, **k):
        called["n"] += 1
        return StockDecisionOutput(ticker="NVDA", verdict="HOLD", confidence="HIGH",
                                   reason="", evidence=[], data_gaps=[])

    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_MISS_RATIONALE, decide=decide, fetchers={},
    )
    assert res.routed and not res.surfaced and res.outcome == OUTCOME_REJECTED
    assert called["n"] == 0, "blind re-derivation must not run once the gate defends"
    assert _stance(sess).stance == "SELL"


def test_spmv_routine_hold_review_does_not_downgrade_plan_sell(sess):
    """A ROUTINE hold review (outcome 'hold', verdict HOLD) must NOT move a plan
    SELL. The override predicate is UNCHANGED by Phase 2 — SPMV fully intact."""
    from datetime import datetime, timezone

    sess.add(HoldingReview(
        user_id="ariel", symbol="NVDA", reviewed_at=datetime.now(timezone.utc),
        verdict="HOLD", confidence="MED", reason="thesis intact",
        evidence_json="{}", position_usd=None, elevated_by_flag=False, outcome="hold",
    ))
    sess.commit()
    kept = _stance(sess)
    assert kept.stance == "SELL", "SPMV: a routine HOLD review may not downgrade a plan SELL"


def test_no_label_no_routing(sess):
    _seed_settled_sell_with_falsifier(sess)
    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary="Holding — thesis intact, mirroring the plan pace.",
        decide=_stub_decide("HOLD"), fetchers={},
    )
    assert not res.routed
    assert sess.query(HoldingReview).count() == 0
    assert _stance(sess).stance == "SELL"


def test_no_standing_reduce_stance_no_routing(tmp_path, monkeypatch):
    """Label present but the standing stance is HOLD (not SELL/TRIM) → no routing."""
    engine = create_engine(f"sqlite:///{tmp_path / 'stance_rev2.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    hold_card = PositionThesis(
        ticker="NVDA", current_shares=100.0, current_weight_pct=10.0,
        current_usd_value=100_000.0, verdict="HOLD", conviction="MED",
        reasoning_md="intact", cited_sources=[], target_weight_pct=None, target_shares=None,
    )
    monkeypatch.setattr(ps, "_load_plan_version", lambda db, uid: SimpleNamespace(id=1, decision_run_id=None))
    monkeypatch.setattr(ps, "_load_portfolio_snapshot", lambda uid, db=None: SimpleNamespace(
        positions=[{"symbol": "NVDA", "shares": 100, "usd_value_k": 100}], as_of=None))
    monkeypatch.setattr(ps, "_plan_theses", lambda *a, **k: [hold_card])

    res = route_stance_revision(
        s, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE, decide=_stub_decide("HOLD"), fetchers={},
    )
    assert not res.routed


def test_fail_closed_blind_raises_keeps_sell(sess):
    """Blind call raises → rejected, stance stays SELL, no exception escapes."""
    _seed_settled_sell_with_falsifier(sess)

    def boom(*a, **k):
        raise RuntimeError("live LLM exploded")

    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE, decide=boom, fetchers={},
    )
    assert res.routed and not res.surfaced and res.outcome == OUTCOME_REJECTED
    kept = _stance(sess)
    assert kept.stance == "SELL"
    assert kept.divergence is True


# ---- FILTER — not gameable (Sol's repro) --------------------------------- #

_EMPTY_RATIONALE = "Holding. PROPOSED STANCE REVISION:    "
_WHITESPACE_RATIONALE = "Holding. PROPOSED STANCE REVISION: --- ... , "
# Sol's opposite-polarity / fabricated fact: lexically matches the falsifier
# "china export ban lifted" while ASSERTING the opposite (ban remains).
_REVERSED_RATIONALE = (
    "Holding. PROPOSED STANCE REVISION: reports that the China export ban was "
    "lifted are false — the ban remains fully in place."
)


def test_sol_repro_empty_revision_no_settled_verdict_rejects(sess):
    """Sol's BLOCKER repro: standing plan SELL (no settled verdict) + EMPTY
    revision label + blind HOLD used to auto-survive (gate allowed by default).
    Now → REJECTED, stance stays SELL, divergence=True."""
    # NOTE: deliberately NO settled verdict seeded — the SELL is plan-only.
    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_EMPTY_RATIONALE, decide=_stub_decide("HOLD"), fetchers={},
    )
    assert res.routed and not res.surfaced and res.outcome == OUTCOME_REJECTED
    assert res.reason == "empty_or_trivial_facts"
    kept = _stance(sess)
    assert kept.stance == "SELL" and kept.divergence is True


def test_whitespace_only_revision_rejects(sess):
    _seed_settled_sell_with_falsifier(sess)
    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_WHITESPACE_RATIONALE, decide=_stub_decide("HOLD"), fetchers={},
    )
    assert res.routed and not res.surfaced and res.reason == "empty_or_trivial_facts"
    assert _stance(sess).stance == "SELL"


def test_reversed_polarity_fact_still_only_surfaces_never_moves(sess):
    """Sol's core concern: a lexically-matching but REVERSED fact can pass the
    lexical gate + a stubbed concurring blind pass — but because Phase 2 only
    SURFACES (never auto-moves), the stance still STAYS SELL. Ariel decides."""
    _seed_settled_sell_with_falsifier(sess)
    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_REVERSED_RATIONALE, decide=_stub_decide("HOLD"), fetchers={},
    )
    # Even in the worst case where the lexical gate + blind both "pass", the
    # money-critical stance does not move — it is only surfaced for approval.
    assert _stance(sess).stance == "SELL"


def test_no_settled_verdict_nonempty_facts_rejects(sess):
    """Substantive facts BUT no settled verdict to test against (gate reason
    no_settled_verdict, not a tripwire hit) + blind HOLD → REJECTED. No
    pre-committed tripwire ⇒ no basis to surface a revision."""
    # No settled verdict seeded.
    called = {"n": 0}

    def decide(*a, **k):
        called["n"] += 1
        return StockDecisionOutput(ticker="NVDA", verdict="HOLD", confidence="HIGH",
                                   reason="", evidence=[], data_gaps=[])

    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE, decide=decide, fetchers={},
    )
    assert res.routed and not res.surfaced and res.outcome == OUTCOME_REJECTED
    assert res.reason == "pushback_no_tripwire_hit"
    assert called["n"] == 0, "blind pass must not run without a positive tripwire hit"
    assert _stance(sess).stance == "SELL"


# ---- fail-closed on persistence (WATERMARK) ------------------------------ #

def test_proposed_write_not_persisted_does_not_surface(sess):
    """record write persists nothing → NOT reported surfaced, stance stays SELL."""
    _seed_settled_sell_with_falsifier(sess)

    def noop_record(*a, **k):
        return None  # simulate a swallowed commit failure — nothing lands

    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE, decide=_stub_decide("HOLD"),
        fetchers={}, record=noop_record,
    )
    assert not res.surfaced, "must not report surfaced when the overlay did not persist"
    assert _stance(sess).stance == "SELL"


def test_watermark_read_failure_reports_not_persisted(sess, monkeypatch):
    """WATERMARK NULL-GUARD: if the pre-write watermark read FAILS (returns None,
    not 0), persistence must be reported False — a stale id>0 row must not
    masquerade as this run's write. → NOT surfaced, stance stays SELL."""
    from argosy.decisions import stance_revision as sr

    _seed_settled_sell_with_falsifier(sess)
    # A stale prior revision_proposed row (id > 0) that a 0-watermark would wrongly accept.
    from datetime import datetime, timezone
    sess.add(HoldingReview(
        user_id="ariel", symbol="NVDA", reviewed_at=datetime.now(timezone.utc),
        verdict="HOLD", confidence="MED", reason="stale prior", evidence_json="{}",
        position_usd=None, elevated_by_flag=False, outcome=OUTCOME_PROPOSED,
    ))
    sess.commit()
    # Simulate a transient watermark-read failure.
    monkeypatch.setattr(sr, "_max_review_id", lambda *a, **k: None)

    res = sr.route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE, decide=_stub_decide("HOLD"), fetchers={},
    )
    assert not res.surfaced, "a failed watermark read must not report the write persisted"
    assert _stance(sess).stance == "SELL"


def test_persistence_watermark_ignores_stale_prior_row(sess):
    """WATERMARK: a stale PRIOR revision_proposed row must NOT be read as this
    run's persisted write. This run's record writes nothing → NOT surfaced."""
    from datetime import datetime, timezone

    _seed_settled_sell_with_falsifier(sess)
    # A stale prior revision_proposed row for the same symbol (older id).
    sess.add(HoldingReview(
        user_id="ariel", symbol="NVDA", reviewed_at=datetime.now(timezone.utc),
        verdict="HOLD", confidence="MED", reason="stale prior", evidence_json="{}",
        position_usd=None, elevated_by_flag=False, outcome=OUTCOME_PROPOSED,
    ))
    sess.commit()

    def noop_record(*a, **k):
        return None  # THIS run persists nothing

    res = route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE, decide=_stub_decide("HOLD"),
        fetchers={}, record=noop_record,
    )
    assert not res.surfaced, "stale prior row must not satisfy the watermark check"
    assert _stance(sess).stance == "SELL"


# ---- TOCTOU: stance changed before the write ----------------------------- #

def test_toctou_stance_flipped_before_write_aborts(sess, monkeypatch):
    """Standing stance re-read immediately before the write is no longer a
    reduction → abort (no revision_proposed overlay)."""
    _seed_settled_sell_with_falsifier(sess)
    from argosy.decisions import stance_revision as sr

    calls = {"n": 0}

    def flaky(db, user_id, ticker):
        calls["n"] += 1
        return "SELL" if calls["n"] == 1 else None  # gone by the pre-write re-check

    monkeypatch.setattr(sr, "_standing_reduce_stance", flaky)
    res = sr.route_stance_revision(
        sess, user_id="ariel", ticker="NVDA",
        rationale_summary=_HIT_RATIONALE, decide=_stub_decide("HOLD"), fetchers={},
    )
    assert res.routed and not res.surfaced
    assert res.reason == "stance_changed_before_write"
    assert sess.query(HoldingReview).filter_by(outcome=OUTCOME_PROPOSED).count() == 0


# ---- rebuild_stances surfacing branch (NO stance move) ------------------- #

def _rebuild_with_card(tmp_path, monkeypatch, card, review_kwargs):
    from datetime import datetime, timezone

    engine = create_engine(f"sqlite:///{tmp_path / 'rev_guard.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    s.add(HoldingReview(
        user_id="ariel", symbol="NVDA", reviewed_at=datetime.now(timezone.utc),
        confidence="HIGH", reason="r", evidence_json="{}", position_usd=None,
        elevated_by_flag=False, **review_kwargs,
    ))
    s.commit()
    monkeypatch.setattr(ps, "_load_plan_version", lambda db, uid: SimpleNamespace(id=1, decision_run_id=None))
    monkeypatch.setattr(ps, "_load_portfolio_snapshot", lambda uid, db=None: SimpleNamespace(
        positions=[{"symbol": "NVDA", "shares": 100, "usd_value_k": 100}], as_of=None))
    monkeypatch.setattr(ps, "_plan_theses", lambda *a, **k: [card])
    rows = rebuild_stances(s, "ariel")
    return next(r for r in rows if r.symbol == "NVDA")


def test_revision_proposed_on_plan_sell_surfaces_but_keeps_sell(tmp_path, monkeypatch):
    """A revision_proposed/HOLD review on a plan SELL → divergence=True, stance
    STAYS SELL (surfaced for approval, never auto-moved)."""
    row = _rebuild_with_card(
        tmp_path, monkeypatch, _sell_card(),
        {"verdict": "HOLD", "outcome": OUTCOME_PROPOSED},
    )
    assert row.stance == "SELL", "revision_proposed must NOT move the stance off plan SELL"
    assert row.divergence is True


def test_revision_proposed_on_plan_buy_does_not_downgrade(tmp_path, monkeypatch):
    """A stale revision_proposed/HOLD review must NOT downgrade a plan BUY —
    the branch only surfaces a divergence, it never overrides the plan stance."""
    buy_card = PositionThesis(
        ticker="NVDA", current_shares=100.0, current_weight_pct=5.0,
        current_usd_value=100_000.0, verdict="BUY", conviction="MED",
        reasoning_md="underweight", cited_sources=[], target_weight_pct=12.0, target_shares=None,
    )
    row = _rebuild_with_card(
        tmp_path, monkeypatch, buy_card,
        {"verdict": "HOLD", "outcome": OUTCOME_PROPOSED},
    )
    assert row.stance == "BUY", "revision_proposed must never override a plan BUY"


def test_open_proposal_still_moves_stance_off_sell(tmp_path, monkeypatch):
    """The ONLY path that moves the stance off plan SELL = Ariel approving, i.e.
    an OPEN proposal overlay (proposal > plan) — unchanged by Phase 2."""
    from argosy.state.models import Proposal

    engine = create_engine(f"sqlite:///{tmp_path / 'rev_approve.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    s.add(Proposal(
        user_id="ariel", ticker="NVDA", action="buy", status="awaiting_human",
        size_shares_or_currency=0.0, size_units="shares", confidence="HIGH", tier="T1",
    ))
    s.commit()
    monkeypatch.setattr(ps, "_load_plan_version", lambda db, uid: SimpleNamespace(id=1, decision_run_id=None))
    monkeypatch.setattr(ps, "_load_portfolio_snapshot", lambda uid, db=None: SimpleNamespace(
        positions=[{"symbol": "NVDA", "shares": 100, "usd_value_k": 100}], as_of=None))
    monkeypatch.setattr(ps, "_plan_theses", lambda *a, **k: [_sell_card()])
    rows = rebuild_stances(s, "ariel")
    row = next(r for r in rows if r.symbol == "NVDA")
    assert row.stance_source == "proposal", "an approved/open proposal is the move path"
    assert row.stance != "SELL"
