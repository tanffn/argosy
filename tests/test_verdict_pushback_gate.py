"""API / loop wiring tests for the verdict pushback gate (Item B)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import ActionProposal, PositionStance, User, Verdict


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


@pytest.mark.asyncio
async def test_run_decision_flow_defends_without_agent_spawn(session, monkeypatch):
    """Synthetic pushback without new facts → status=defended, zero flow.run calls."""
    from argosy.api.routes import decisions as decisions_mod
    from argosy.services.verdict_registry import write_verdict

    write_verdict(
        session, user_id="ariel", subject="NOW",
        verdict="SELL", conviction="HIGH",
        # Substantive (not thin) so the HOLE#33 floor keeps it DEFENDED: >=2
        # non-degenerate falsifiers + a long rationale.
        falsifiers=[
            "GAAP profitability turns sustainably positive for two quarters",
            "net revenue retention recovers above 115% with margins stable",
        ],
        reasoning_md=(
            "NOW is priced for flawless execution; the deep-decision fleet held "
            "SELL on a valuation-vs-growth basis after a two-round debate and a "
            "three-perspective risk pass. The thesis is documented at length and "
            "the falsifiers below are the specific, dated conditions that would "
            "reopen it."
        ),
        source_decision_run_id=164,
    )
    session.commit()

    # Point the route's sync engine at the test DB.
    monkeypatch.setattr(
        "argosy.state.db.get_engine",
        lambda: session.get_bind(),
    )

    ran = {"n": 0}

    async def _boom(*a, **k):
        ran["n"] += 1
        raise AssertionError("DecisionFlow.run must not be called when DEFENDED")

    monkeypatch.setattr(
        decisions_mod.DecisionFlow, "run", _boom,
    )
    monkeypatch.setattr(
        decisions_mod, "load_agent_settings",
        lambda user_id: type("S", (), {"tiers": type("T", (), {"cooling_off_hours_t3": 24})()})(),
    )

    body = decisions_mod.RunRequest(
        user_id="ariel", ticker="NOW", tier="T2",
        cited_new_facts=["Ariel thinks x2-3"],
    )
    resp = await decisions_mod.run_decision_flow(body)
    assert resp.status == "defended"
    assert resp.blocked_by == "verdict_defended"
    assert resp.standing_verdict is not None
    assert resp.standing_verdict["verdict"] == "SELL"
    assert ran["n"] == 0


@pytest.mark.asyncio
async def test_verdict_trigger_loop_writes_unlock_row(session):
    from argosy.orchestrator.loops.verdict_trigger_daily import VerdictTriggerDailyLoop
    from argosy.services.verdict_registry import write_verdict

    write_verdict(
        session, user_id="ariel", subject="ORCL",
        verdict="WAIT", conviction="HIGH",
        revisit_triggers=[{"kind": "price_below", "price": 115.0}],
        source_decision_run_id=198,
    )
    session.commit()

    def _quotes(sess, user_id, subjects):
        return {"ORCL": 110.0}

    SessionLocal = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    loop = VerdictTriggerDailyLoop(
        enabled=True,
        user_id="ariel",
        session_factory=SessionLocal,
        quotes_fn=_quotes,
        today=date(2026, 7, 11),
    )
    out = await loop.tick()
    assert out.get("fired") == 1
    assert out.get("unlock_proposal_ids")
    prop = session.get(ActionProposal, out["unlock_proposal_ids"][0])
    # Refresh from DB via new query.
    SessionLocal2 = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    s2 = SessionLocal2()
    try:
        prop = s2.get(ActionProposal, out["unlock_proposal_ids"][0])
        assert prop is not None
        assert "revisit unlocked: ORCL" in prop.summary
    finally:
        s2.close()


@pytest.mark.asyncio
async def test_verdict_trigger_loop_accepts_scheduler_clock(session):
    """Regression (live-smoke 2026-07-11): the scheduler drives every loop as
    ``await loop.tick(now=self.clock)`` — the loop crashed on its first real
    fire because tick() lacked the kwarg. Drive the loop with the scheduler's
    calling convention and verify a dated trigger fires off that clock (no
    constructor ``today`` pin)."""
    from datetime import datetime, timezone

    from argosy.orchestrator.loops.verdict_trigger_daily import VerdictTriggerDailyLoop
    from argosy.services.verdict_registry import write_verdict

    write_verdict(
        session, user_id="ariel", subject="OKLO",
        verdict="HOLD", conviction="MEDIUM",
        revisit_triggers=[{
            "kind": "dated_event", "date": "2026-07-31",
            "label": "July-2026 first criticality",
        }],
        source_decision_run_id=199,
    )
    session.commit()

    SessionLocal = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    loop = VerdictTriggerDailyLoop(
        enabled=True,
        user_id="ariel",
        session_factory=SessionLocal,
        quotes_fn=lambda sess, user_id, subjects: {},
        # No ``today`` pin — the scheduler clock must supply the as-of date.
    )

    def _clock() -> datetime:
        return datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc)

    out = await loop.tick(now=_clock)  # the scheduler's exact convention
    assert "error" not in out
    assert out.get("fired") == 1
    assert out.get("unlock_proposal_ids")

    # Before the dated event, the same convention fires nothing.
    def _early() -> datetime:
        return datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc)

    out2 = await loop.tick(now=_early)
    assert "error" not in out2
    assert out2.get("fired") == 0


@pytest.mark.asyncio
async def test_verdict_trigger_loop_default_quotes_path(session):
    """Live-smoke 2026-07-12: every prior test injected quotes_fn, so the
    module-default fetcher's signature mismatch was never caught. Run the
    loop WITHOUT injecting quotes_fn (default path) — it must not error."""
    from argosy.orchestrator.loops.verdict_trigger_daily import VerdictTriggerDailyLoop
    from argosy.services.verdict_registry import write_verdict

    write_verdict(
        session, user_id="ariel", subject="ORCL",
        verdict="WAIT", conviction="HIGH",
        revisit_triggers=[{"kind": "price_below", "price": 115.0}],
        source_decision_run_id=198,
    )
    session.commit()

    SessionLocal = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    loop = VerdictTriggerDailyLoop(
        enabled=True, user_id="ariel", session_factory=SessionLocal,
        today=date(2026, 7, 12),
        # no quotes_fn — exercise the module default
    )
    out = await loop.tick()
    assert "error" not in out, out
    assert out.get("subjects") == 1


# --------------------------------------------------------------------------- #
# Phase 3 — stale-verdict-contradicts-stance forced re-derivation (SEAM 1).
# --------------------------------------------------------------------------- #

# Anchored RELATIVE to now so these stance-focused cases stay well inside the
# HOLE#33 staleness horizon (45d) regardless of the calendar — they isolate the
# stale-stance logic, not the freshness floor.
_NOW = datetime.now(timezone.utc)
_T0 = _NOW - timedelta(days=6)
_T1 = _T0 + timedelta(days=1)
_T2 = _T0 + timedelta(days=2)

# A rationale long enough to clear the HOLE#33 substance floor (>=200 chars).
_SUBSTANTIVE_REASONING = (
    "Deep-decision fleet verdict after a full two-round bull/bear debate and a "
    "three-perspective risk pass. The thesis, the position sizing, and the exit "
    "conditions are all documented here at length so this verdict carries real "
    "authority and is not a one-line seed."
)
# Two distinct, non-degenerate falsifiers → clears the >=2 substance floor.
_SUBSTANTIVE_FALSIFIERS = [
    "TTM free cash flow turns sustainably negative for a full year",
    "net revenue retention falls below 110% for two consecutive quarters",
]


def _seed_verdict(
    session, *, subject, verdict, updated_at, falsifiers=None, reasoning_md=None
):
    # Default to a SUBSTANTIVE verdict so stance-focused tests are not swept up
    # by the HOLE#33 thin/stale floor; thin cases pass explicit short values.
    if falsifiers is None:
        falsifiers = list(_SUBSTANTIVE_FALSIFIERS)
    if reasoning_md is None:
        reasoning_md = _SUBSTANTIVE_REASONING
    v = Verdict(
        user_id="ariel",
        subject=subject,
        verdict=verdict,
        conviction="HIGH",
        settled=True,
        falsifiers_json=json.dumps(falsifiers) if falsifiers else None,
        reasoning_md=reasoning_md,
        created_at=updated_at,
        updated_at=updated_at,  # explicit → onupdate does not override on INSERT
    )
    session.add(v)
    session.commit()
    return v


def _seed_stance(session, *, symbol, stance, built_at):
    st = PositionStance(
        user_id="ariel",
        symbol=symbol,
        stance=stance,
        stance_source="plan",
        conviction="HIGH",
        plan_verdict=stance,
        reasoning_md="",
        divergence=False,
        built_at=built_at,
    )
    session.add(st)
    session.commit()
    return st


def test_stale_keep_verdict_predates_reduce_stance_forces_once(session):
    """Settled HOLD updated BEFORE a SELL stance → forced re-derivation, ONCE."""
    from argosy.services.verdict_registry import check_pushback_gate

    v = _seed_verdict(session, subject="NVDA", verdict="HOLD", updated_at=_T0)
    _seed_stance(session, symbol="NVDA", stance="SELL", built_at=_T1)

    gate = check_pushback_gate(session, user_id="ariel", subject="NVDA")
    assert gate.allowed is True
    assert gate.reason == "stale_verdict_contradicts_stance"
    assert gate.standing is not None and gate.standing.verdict == "HOLD"

    # Loop bound: a forced re-run bumps updated_at to now (>= built_at) → the
    # gate DEFENDS again. Simulate that bump and re-check: no repeat force.
    v.updated_at = _T2
    session.commit()
    gate2 = check_pushback_gate(session, user_id="ariel", subject="NVDA")
    assert gate2.allowed is False
    assert gate2.reason.startswith("DEFENDED")


def test_fresh_verdict_after_stance_defends(session):
    """Verdict updated AFTER the stance's built_at → DEFENDED (it saw it)."""
    from argosy.services.verdict_registry import check_pushback_gate

    _seed_verdict(session, subject="NVDA", verdict="HOLD", updated_at=_T1)
    _seed_stance(session, symbol="NVDA", stance="SELL", built_at=_T0)

    gate = check_pushback_gate(session, user_id="ariel", subject="NVDA")
    assert gate.allowed is False
    assert gate.reason.startswith("DEFENDED")


def test_equal_timestamp_not_forced(session):
    """updated_at == built_at → NOT forced (boundary fails SAFE)."""
    from argosy.services.verdict_registry import check_pushback_gate

    _seed_verdict(session, subject="NVDA", verdict="HOLD", updated_at=_T1)
    _seed_stance(session, symbol="NVDA", stance="SELL", built_at=_T1)

    gate = check_pushback_gate(session, user_id="ariel", subject="NVDA")
    assert gate.allowed is False


def test_no_stance_row_defends(session):
    from argosy.services.verdict_registry import check_pushback_gate

    _seed_verdict(session, subject="NVDA", verdict="HOLD", updated_at=_T0)
    gate = check_pushback_gate(session, user_id="ariel", subject="NVDA")
    assert gate.allowed is False


def test_keep_stance_not_forced(session):
    """Stance is a KEEP verb (HOLD) → not a contested reduction → DEFENDED."""
    from argosy.services.verdict_registry import check_pushback_gate

    _seed_verdict(session, subject="NVDA", verdict="HOLD", updated_at=_T0)
    _seed_stance(session, symbol="NVDA", stance="HOLD", built_at=_T1)
    gate = check_pushback_gate(session, user_id="ariel", subject="NVDA")
    assert gate.allowed is False


def test_reduce_verdict_not_forced(session):
    """Settled verdict is itself a REDUCE verb → nothing to reconcile → DEFENDED."""
    from argosy.services.verdict_registry import check_pushback_gate

    _seed_verdict(session, subject="NVDA", verdict="SELL", updated_at=_T0)
    _seed_stance(session, symbol="NVDA", stance="SELL", built_at=_T1)
    gate = check_pushback_gate(session, user_id="ariel", subject="NVDA")
    assert gate.allowed is False


def test_positive_tripwire_precedes_stale_reason(session):
    """A cited fact that hits a recorded falsifier wins the POSITIVE reason even
    when a stale stance also exists (Phase 2 must still see a tripwire hit)."""
    from argosy.services.verdict_registry import check_pushback_gate

    _seed_verdict(
        session, subject="NVDA", verdict="HOLD", updated_at=_T0,
        falsifiers=["GAAP profitability lost"],
    )
    _seed_stance(session, symbol="NVDA", stance="SELL", built_at=_T1)
    gate = check_pushback_gate(
        session, user_id="ariel", subject="NVDA",
        cited_new_facts=["GAAP profitability lost this quarter"],
    )
    assert gate.allowed is True
    assert gate.reason == "new_fact_hits_falsifier"


def test_predates_tz_and_boundary():
    """_predates: tz naive-vs-aware compares without error; equal/None → False."""
    from argosy.services.verdict_registry import _predates

    aware = datetime(2026, 7, 2, tzinfo=timezone.utc)
    naive_older = datetime(2026, 7, 1)
    assert _predates(naive_older, aware) is True  # no TypeError
    assert _predates(aware, aware) is False  # equal boundary → not forced
    assert _predates(None, aware) is False
    assert _predates(aware, None) is False


def test_phase3_reason_not_a_positive_gate_reason():
    """Phase 2 must keep REJECTING the new reason (not a committed-tripwire hit)."""
    from argosy.decisions.stance_revision import _POSITIVE_GATE_REASONS

    assert "stale_verdict_contradicts_stance" not in _POSITIVE_GATE_REASONS


# --------------------------------------------------------------------------- #
# HOLE #33 — freshness / substance authority floor on the DEFENDED path.
# --------------------------------------------------------------------------- #


def test_thin_but_just_written_verdict_defends(session):
    """DEFECT 1 loop bound: a THIN verdict written just now (age < _THIN_MIN_AGE)
    must DEFEND — a freshly force-rewritten still-thin row is not re-forced."""
    from argosy.services.verdict_registry import check_pushback_gate

    _seed_verdict(
        session,
        subject="BMY",
        verdict="HOLD",
        updated_at=datetime.now(timezone.utc),  # minutes old → within settle window
        falsifiers=["thesis changes"],
        reasoning_md="BMY HOLD (run 187) — settled deep-decision",  # thin
    )
    gate = check_pushback_gate(session, user_id="ariel", subject="BMY")
    assert gate.allowed is False
    assert gate.reason.startswith("DEFENDED")


def test_thin_and_old_verdict_forces_once_then_defends(session):
    """A THIN seed older than _THIN_MIN_AGE (30 days) → forced ONCE; after the
    forced re-run writes a SUBSTANTIVE fresh verdict, the gate DEFENDS."""
    from argosy.services.verdict_registry import check_pushback_gate

    v = _seed_verdict(
        session,
        subject="BMY",
        verdict="HOLD",
        updated_at=datetime.now(timezone.utc) - timedelta(days=30),  # stale-thin seed
        falsifiers=["thesis changes"],  # 1 vague/degenerate falsifier
        reasoning_md="BMY HOLD (run 187) — settled deep-decision",  # 43-ish chars
    )
    gate = check_pushback_gate(session, user_id="ariel", subject="BMY")
    assert gate.allowed is True
    assert gate.reason == "thin_or_stale_verdict"
    assert gate.standing is not None and gate.standing.verdict == "HOLD"

    # Simulate the forced re-run persisting a substantive, fresh verdict.
    v.updated_at = datetime.now(timezone.utc)
    v.reasoning_md = _SUBSTANTIVE_REASONING
    v.falsifiers_json = json.dumps(_SUBSTANTIVE_FALSIFIERS)
    session.commit()
    gate2 = check_pushback_gate(session, user_id="ariel", subject="BMY")
    assert gate2.allowed is False
    assert gate2.reason.startswith("DEFENDED")


def test_fresh_substantive_verdict_defends(session):
    """A FRESH, substantive verdict (long reasoning, >=2 real falsifiers, recent
    updated_at) → still DEFENDED (the floor must not churn real winners)."""
    from argosy.services.verdict_registry import check_pushback_gate

    _seed_verdict(
        session,
        subject="AMD",
        verdict="HOLD",
        updated_at=datetime.now(timezone.utc) - timedelta(days=4),  # AMD/NOW at 4d
    )
    gate = check_pushback_gate(session, user_id="ariel", subject="AMD")
    assert gate.allowed is False
    assert gate.reason.startswith("DEFENDED")


def test_stale_substantive_verdict_forces_once_then_defends(session):
    """A substantive but STALE verdict (updated_at 60d ago) → forced ONCE; after
    a re-run bumps updated_at to now, the fresh verdict DEFENDS (loop-bound)."""
    from argosy.services.verdict_registry import check_pushback_gate

    v = _seed_verdict(
        session,
        subject="MDT",
        verdict="HOLD",
        updated_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    gate = check_pushback_gate(session, user_id="ariel", subject="MDT")
    assert gate.allowed is True
    assert gate.reason == "thin_or_stale_verdict"

    # Loop bound: a forced re-run calls write_verdict → updated_at bumps to now.
    v.updated_at = datetime.now(timezone.utc)
    session.commit()
    gate2 = check_pushback_gate(session, user_id="ariel", subject="MDT")
    assert gate2.allowed is False
    assert gate2.reason.startswith("DEFENDED")


def test_thin_or_stale_tz_naive_updated_at_is_safe(session):
    """A tz-NAIVE updated_at (SQLite round-trip) must not crash the age compare —
    a 60d-old naive timestamp still trips STALE."""
    from argosy.services.verdict_registry import check_pushback_gate

    naive_old = (datetime.now(timezone.utc) - timedelta(days=60)).replace(tzinfo=None)
    _seed_verdict(session, subject="LLY", verdict="HOLD", updated_at=naive_old)
    gate = check_pushback_gate(session, user_id="ariel", subject="LLY")
    assert gate.allowed is True
    assert gate.reason == "thin_or_stale_verdict"


def test_thin_or_stale_reason_not_a_positive_gate_reason():
    """The forced-re-run reason must NOT count as a committed-tripwire hit, so
    Phase 2 stance-revision keeps REJECTING on it (not surface a revision)."""
    from argosy.decisions.stance_revision import _POSITIVE_GATE_REASONS

    assert "thin_or_stale_verdict" not in _POSITIVE_GATE_REASONS
