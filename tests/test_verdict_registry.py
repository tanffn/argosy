"""Verdict registry + pushback gate + trigger checker unit tests."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import ActionProposal, User, Verdict


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def test_migration_creates_verdicts_table(session):
    assert session.query(Verdict).count() == 0


def test_write_and_get_settled_verdict(session):
    from argosy.services.verdict_registry import get_settled_verdict, write_verdict

    row = write_verdict(
        session,
        user_id="ariel",
        subject="orcl",
        verdict="HOLD",
        conviction="HIGH",
        falsifiers=["FCF turns sustainably positive with debt stable"],
        revisit_triggers=[
            {"kind": "price_below", "price": 110.0},
            {"kind": "price_below", "price": 115.0},
            {
                "kind": "metric_condition",
                "metric": "fcf_ttm",
                "op": ">",
                "value": 0,
                "label": "FCF positive with debt stable",
            },
        ],
        next_validation=date(2026, 10, 1),
        source_decision_run_id=198,
        reasoning_md="Fair value ~$150 vs ~$140.6; HOLD/wait.",
    )
    session.commit()
    got = get_settled_verdict(session, user_id="ariel", subject="ORCL")
    assert got is not None and got.id == row.id
    assert got.verdict == "HOLD"
    assert got.conviction == "HIGH"
    assert got.settled is True


def test_write_supersedes_prior_settled(session):
    from argosy.services.verdict_registry import get_settled_verdict, write_verdict

    first = write_verdict(
        session, user_id="ariel", subject="SOFI",
        verdict="HOLD", conviction="MED",
        source_decision_run_id=186,
    )
    session.commit()
    second = write_verdict(
        session, user_id="ariel", subject="SOFI",
        verdict="SELL", conviction="HIGH",
        source_decision_run_id=200,
    )
    session.commit()
    session.refresh(first)
    assert first.settled is False
    assert first.superseded_by == second.id
    standing = get_settled_verdict(session, user_id="ariel", subject="SOFI")
    assert standing is not None and standing.id == second.id
    assert standing.verdict == "SELL"


def test_pushback_without_new_facts_is_defended(session):
    from argosy.services.verdict_registry import check_pushback_gate, write_verdict

    write_verdict(
        session, user_id="ariel", subject="NOW",
        verdict="SELL", conviction="HIGH",
        falsifiers=["revenue re-acceleration above 20% YoY"],
        source_decision_run_id=165,
    )
    session.commit()
    gate = check_pushback_gate(
        session, user_id="ariel", subject="NOW", cited_new_facts=None,
    )
    assert gate.defended is True
    assert gate.allowed is False
    assert gate.standing is not None
    assert gate.standing.verdict == "SELL"
    assert "DEFENDED" in gate.reason


def test_pushback_with_matching_falsifier_allows(session):
    from argosy.services.verdict_registry import check_pushback_gate, write_verdict

    write_verdict(
        session, user_id="ariel", subject="NOW",
        verdict="SELL", conviction="HIGH",
        falsifiers=["revenue re-acceleration above 20% YoY"],
        source_decision_run_id=165,
    )
    session.commit()
    gate = check_pushback_gate(
        session, user_id="ariel", subject="NOW",
        cited_new_facts=[
            "Q2 print showed revenue re-acceleration above 20% YoY"
        ],
    )
    assert gate.allowed is True
    assert gate.matched_falsifier is not None


def test_pushback_with_unrelated_facts_still_defended(session):
    from argosy.services.verdict_registry import check_pushback_gate, write_verdict

    write_verdict(
        session, user_id="ariel", subject="NOW",
        verdict="SELL", conviction="HIGH",
        falsifiers=["revenue re-acceleration above 20% YoY"],
        source_decision_run_id=165,
    )
    session.commit()
    gate = check_pushback_gate(
        session, user_id="ariel", subject="NOW",
        cited_new_facts=["Ariel thinks it could 2-3x from here"],
    )
    assert gate.defended is True


def test_trigger_checker_fires_on_price_cross(session):
    from argosy.services.verdict_registry import (
        evaluate_triggers,
        write_unlock_inbox_rows,
        write_verdict,
    )

    write_verdict(
        session, user_id="ariel", subject="ORCL",
        verdict="WAIT", conviction="HIGH",
        revisit_triggers=[{"kind": "price_below", "price": 115.0}],
        source_decision_run_id=198,
    )
    session.commit()
    fired = evaluate_triggers(
        session, user_id="ariel", quotes={"ORCL": 112.0},
    )
    assert len(fired) == 1
    assert fired[0].subject == "ORCL"
    assert "112" in fired[0].evidence
    ids = write_unlock_inbox_rows(session, user_id="ariel", fired=fired)
    session.commit()
    assert len(ids) == 1
    prop = session.get(ActionProposal, ids[0])
    assert prop is not None
    assert prop.kind == "note_only"
    assert prop.status == "open"
    assert "revisit unlocked: ORCL" in prop.summary
    assert prop.dedup_key.startswith("verdict_revisit_unlocked:ORCL:")


def test_dated_event_trigger_fires(session):
    from argosy.services.verdict_registry import evaluate_triggers, write_verdict

    write_verdict(
        session, user_id="ariel", subject="OKLO",
        verdict="HOLD", conviction="MED",
        revisit_triggers=[{
            "kind": "dated_event",
            "date": "2026-07-15",
            "label": "first criticality",
        }],
        source_decision_run_id=199,
    )
    session.commit()
    fired = evaluate_triggers(
        session, user_id="ariel", today=date(2026, 7, 20),
    )
    assert len(fired) == 1
    assert "first criticality" in fired[0].evidence


def test_sleeve_fit_blocks_run166_adjacent():
    from argosy.services.verdict_registry import check_sleeve_fit

    bad = check_sleeve_fit(
        action="BUY", named_sleeve="high-potential-ADJACENT", subject="NOW",
    )
    assert bad.ok is False
    assert "run-166" in bad.reason

    missing = check_sleeve_fit(action="BUY", named_sleeve=None, subject="NOW")
    assert missing.ok is False

    ok = check_sleeve_fit(action="BUY", named_sleeve="alpha", subject="RKT")
    assert ok.ok is True

    hold = check_sleeve_fit(action="HOLD", named_sleeve=None, subject="ORCL")
    assert hold.ok is True


def test_blind_valuation_required_on_buy():
    from argosy.services.verdict_registry import require_blind_valuation_rederivation

    miss = require_blind_valuation_rederivation(action="BUY", live_inputs={})
    assert miss.ok is False

    price_only = require_blind_valuation_rederivation(
        action="BUY", live_inputs={"price": 40.0},
    )
    assert price_only.ok is False

    ok = require_blind_valuation_rederivation(
        action="BUY",
        live_inputs={"price": 40.0, "fair_value": 28.0, "pe": 12.0},
    )
    assert ok.ok is True
    assert ok.derived["price"] == 40.0

    hold = require_blind_valuation_rederivation(action="HOLD", live_inputs=None)
    assert hold.ok is True


def test_buy_structural_gates_soft_when_fields_absent():
    """Default rollout: missing funnel_meta → soft pass + would-block warnings."""
    from argosy.services.verdict_registry import evaluate_buy_structural_gates

    soft = evaluate_buy_structural_gates(
        action="BUY",
        subject="AAPL",
        named_sleeve=None,
        live_valuation={},
        enforce=False,
    )
    assert soft.block is False
    assert soft.reason == "soft_pass"
    assert any("sleeve_fit_invalid" in w for w in soft.warnings)
    assert any("valuation_rederivation_failed" in w for w in soft.warnings)


def test_buy_structural_gates_enforce_flag_hard_blocks():
    from argosy.services.verdict_registry import evaluate_buy_structural_gates

    hard = evaluate_buy_structural_gates(
        action="BUY",
        subject="AAPL",
        named_sleeve=None,
        live_valuation={},
        enforce=True,
    )
    assert hard.block is True
    assert hard.blocked_by == "sleeve_fit_invalid"


def test_buy_structural_gates_supplied_bad_sleeve_blocks_even_when_soft():
    """Caller-supplied adjacent sleeve still hard-blocks (run-166 class)."""
    from argosy.services.verdict_registry import evaluate_buy_structural_gates

    bad = evaluate_buy_structural_gates(
        action="BUY",
        subject="NOW",
        named_sleeve="high-potential-ADJACENT",
        live_valuation={"price": 50.0, "fair_value": 40.0},
        enforce=False,
    )
    assert bad.block is True
    assert bad.blocked_by == "sleeve_fit_invalid"
    assert "run-166" in bad.reason


def test_run166_now_failure_class_replays_blocked(session):
    """Standing SELL + pushback without falsifier hit + adjacent sleeve → blocked."""
    from argosy.services.verdict_registry import (
        check_pushback_gate,
        evaluate_buy_structural_gates,
        write_verdict,
    )

    write_verdict(
        session, user_id="ariel", subject="NOW",
        verdict="SELL", conviction="HIGH",
        falsifiers=["clear path to GAAP profitability"],
        source_decision_run_id=164,
    )
    session.commit()

    # Pushback seeded with Ariel's framing (run-166 shape) — DEFENDED.
    gate = check_pushback_gate(
        session, user_id="ariel", subject="NOW",
        cited_new_facts=["Ariel thinks x2-3"],
    )
    assert gate.defended is True

    # Adjacent sleeve + incomplete valuation → structural block when fields supplied.
    structural = evaluate_buy_structural_gates(
        action="BUY",
        subject="NOW",
        named_sleeve="high-potential-ADJACENT",
        live_valuation={"price": 50.0},
        enforce=False,
    )
    assert structural.block is True
    assert structural.blocked_by == "sleeve_fit_invalid"


def test_seed_catalog_covers_required_subjects():
    from scripts.seed_verdict_registry import SEEDS

    subjects = {s["subject"] for s in SEEDS}
    assert subjects >= {
        "ORCL", "SOFI", "BMY", "OPEN", "VOR", "OKLO", "RKLB", "ASTS",
    }
    orcl = next(s for s in SEEDS if s["subject"] == "ORCL")
    assert orcl["source_decision_run_id"] == 198
    kinds = {t["kind"] for t in orcl["revisit_triggers"]}
    assert "price_below" in kinds
    assert "metric_condition" in kinds
