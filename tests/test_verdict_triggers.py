"""Deterministic verdict-trigger evaluator + firing tests.

All seams (now / quote_fn / macro_fn / decide_fn) are injected — no network,
no LLM, no live DB (the ``session`` fixture is an isolated tmp SQLite at head).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

import argosy.services.verdict_triggers as vt
from argosy.services.verdict_registry import write_verdict
from argosy.services.verdict_triggers import (
    FIRED_DEDUP_PREFIX,
    evaluate_standing_verdict_triggers,
    fire_tripped_triggers,
    sweep_and_fire,
)
from argosy.state.models import ActionProposal, User, Verdict


def _fired_markers(session):
    """Live (open) firing markers only — a released claim is deleted/rejected."""
    return (
        session.query(ActionProposal)
        .filter(
            ActionProposal.dedup_key.like(f"{FIRED_DEDUP_PREFIX}:%"),
            ActionProposal.status == "open",
        )
        .all()
    )


def _verdict_id(session, subject):
    return (
        session.query(Verdict)
        .filter(Verdict.subject == subject.upper(), Verdict.settled.is_(True))
        .one()
        .id
    )

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _write(session, subject, triggers, run_id):
    write_verdict(
        session,
        user_id="ariel",
        subject=subject,
        verdict="HOLD",
        conviction="HIGH",
        revisit_triggers=triggers,
        source_decision_run_id=run_id,
    )
    session.commit()


class _RecordingDecide:
    """Fake escalation seam — records every call, never touches the fleet."""

    def __init__(self):
        self.calls = []

    def __call__(self, *, user_id, subject, cited_new_facts, reason):
        self.calls.append(
            {"subject": subject, "cited": cited_new_facts, "reason": reason}
        )
        return {"status": "reevaluated", "subject": subject}


# ---------------------------------------------------------------------------
# 1) Deterministic evaluator
# ---------------------------------------------------------------------------
def test_dated_event_past_trips(session):
    _write(session, "AAA", [{"kind": "dated_event", "date": "2026-01-01", "label": "lockup expiry"}], 1)
    [res] = evaluate_standing_verdict_triggers(session, "ariel", now=NOW)
    assert res.subject == "AAA" and res.checked == 1
    assert len(res.tripped) == 1 and not res.unevaluable
    assert "lockup expiry" in res.tripped[0].evidence


def test_dated_event_future_does_not_trip(session):
    _write(session, "BBB", [{"kind": "dated_event", "date": "2027-01-01"}], 2)
    [res] = evaluate_standing_verdict_triggers(session, "ariel", now=NOW)
    assert not res.tripped and not res.unevaluable and res.checked == 1


def test_price_below_and_above_vs_injected_quote(session):
    _write(session, "CCC", [{"kind": "price_below", "price": 100.0}], 3)
    _write(session, "DDD", [{"kind": "price_above", "price": 50.0}], 4)
    quotes = {"CCC": 95.0, "DDD": 55.0}
    results = evaluate_standing_verdict_triggers(
        session, "ariel", now=NOW, quote_fn=lambda s: quotes.get(s)
    )
    by = {r.subject: r for r in results}
    assert by["CCC"].tripped and by["DDD"].tripped

    # Flip the quotes: neither should trip now.
    quotes2 = {"CCC": 120.0, "DDD": 40.0}
    results2 = evaluate_standing_verdict_triggers(
        session, "ariel", now=NOW, quote_fn=lambda s: quotes2.get(s)
    )
    by2 = {r.subject: r for r in results2}
    assert not by2["CCC"].tripped and not by2["DDD"].tripped


def test_price_trigger_unevaluable_without_quote(session):
    _write(session, "EEE", [{"kind": "price_below", "price": 100.0}], 5)
    # No quote_fn injected -> unevaluable, NOT falsely not-tripped.
    [res] = evaluate_standing_verdict_triggers(session, "ariel", now=NOW)
    assert not res.tripped and len(res.unevaluable) == 1
    assert "no quote feed" in res.unevaluable[0].reason


def test_metric_condition_no_feed_is_unevaluable(session):
    _write(
        session, "FFF",
        [{"kind": "metric_condition", "metric": "fcf_ttm", "op": ">", "value": 0}],
        6,
    )
    # macro_fn returns None -> the metric is honestly UNEVALUABLE, not skipped.
    [res] = evaluate_standing_verdict_triggers(
        session, "ariel", now=NOW, macro_fn=lambda subj, metric: None
    )
    assert not res.tripped and len(res.unevaluable) == 1
    assert "not available" in res.unevaluable[0].reason
    # And no macro_fn at all is also unevaluable (never silently tripped).
    [res2] = evaluate_standing_verdict_triggers(session, "ariel", now=NOW)
    assert len(res2.unevaluable) == 1


def test_metric_condition_trips_with_feed(session):
    _write(
        session, "GGG",
        [{"kind": "metric_condition", "metric": "fcf_ttm", "op": "<", "value": 0}],
        7,
    )
    [res] = evaluate_standing_verdict_triggers(
        session, "ariel", now=NOW, macro_fn=lambda subj, metric: -5.0
    )
    assert len(res.tripped) == 1 and not res.unevaluable


def test_no_triggers_is_noop(session):
    _write(session, "HHH", [], 8)
    [res] = evaluate_standing_verdict_triggers(session, "ariel", now=NOW)
    assert res.checked == 0 and not res.tripped and not res.unevaluable
    fired = fire_tripped_triggers(session, "ariel", [res], now=NOW, decide_fn=_RecordingDecide())
    assert fired == []


def test_evaluator_is_read_only(session):
    _write(session, "III", [{"kind": "dated_event", "date": "2026-01-01"}], 9)
    before = session.query(ActionProposal).count()
    evaluate_standing_verdict_triggers(session, "ariel", now=NOW)
    assert session.query(ActionProposal).count() == before  # no side effects


# ---------------------------------------------------------------------------
# 2) Firing — escalate once, idempotent, best-effort
# ---------------------------------------------------------------------------
def test_tripped_trigger_escalates_exactly_once(session):
    _write(session, "JJJ", [{"kind": "dated_event", "date": "2026-01-01", "label": "earnings"}], 10)
    decide = _RecordingDecide()

    s1 = sweep_and_fire(session, "ariel", now=NOW, decide_fn=decide)
    session.commit()
    assert s1["escalated"] == 1 and s1["tripped_verdicts"] == 1
    assert len(decide.calls) == 1
    assert len(_fired_markers(session)) == 1
    # The cited fact must reference the trigger so the pushback gate would clear.
    assert "earnings" in decide.calls[0]["cited"][0] or "2026-01-01" in decide.calls[0]["cited"][0]

    # Second sweep: same standing verdict -> NO re-fire (idempotent marker).
    s2 = sweep_and_fire(session, "ariel", now=NOW, decide_fn=decide)
    session.commit()
    assert s2["escalated"] == 0 and s2["skipped_already_fired"] == 1
    assert len(decide.calls) == 1  # unchanged
    assert len(_fired_markers(session)) == 1


def test_new_verdict_re_escalates(session):
    _write(session, "KKK", [{"kind": "dated_event", "date": "2026-01-01"}], 20)
    decide = _RecordingDecide()
    sweep_and_fire(session, "ariel", now=NOW, decide_fn=decide)
    session.commit()
    assert len(decide.calls) == 1

    # A NEW settled verdict (new id) supersedes -> escalatable again.
    _write(session, "KKK", [{"kind": "dated_event", "date": "2026-02-01"}], 21)
    sweep_and_fire(session, "ariel", now=NOW, decide_fn=decide)
    session.commit()
    assert len(decide.calls) == 2


def test_failed_subject_does_not_abort_sweep(session):
    _write(session, "LLL", [{"kind": "dated_event", "date": "2026-01-01"}], 30)
    _write(session, "MMM", [{"kind": "dated_event", "date": "2026-01-01"}], 31)

    def flaky(*, user_id, subject, cited_new_facts, reason):
        if subject == "LLL":
            raise RuntimeError("simulated fleet failure")
        return {"ok": subject}

    summary = sweep_and_fire(session, "ariel", now=NOW, decide_fn=flaky)
    session.commit()
    # MMM still escalated despite LLL failing; error captured, not raised.
    assert summary["escalated"] == 1
    assert any("LLL" in e for e in summary["errors"])
    # Failed subject left NO fired marker -> it is re-tryable next sweep.
    keys = [f.dedup_key for f in _fired_markers(session)]
    assert not any("LLL" in (k or "") for k in keys)
    assert any("MMM" in (k or "") for k in keys)


def test_failed_subject_retries_next_sweep(session):
    _write(session, "NNN", [{"kind": "dated_event", "date": "2026-01-01"}], 40)
    calls = {"n": 0}

    def once_flaky(*, user_id, subject, cited_new_facts, reason):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"ok": subject}

    sweep_and_fire(session, "ariel", now=NOW, decide_fn=once_flaky)
    session.commit()
    # Second sweep retries (no marker was written on failure) and succeeds.
    s2 = sweep_and_fire(session, "ariel", now=NOW, decide_fn=once_flaky)
    session.commit()
    assert calls["n"] == 2 and s2["escalated"] == 1


def test_only_tripped_symbols_escalate(session):
    _write(session, "OOO", [{"kind": "dated_event", "date": "2027-01-01"}], 50)  # future
    _write(session, "PPP", [{"kind": "dated_event", "date": "2026-01-01"}], 51)  # past
    decide = _RecordingDecide()
    summary = sweep_and_fire(session, "ariel", now=NOW, decide_fn=decide)
    session.commit()
    assert summary["escalated"] == 1
    assert [c["subject"] for c in decide.calls] == ["PPP"]


# ---------------------------------------------------------------------------
# Sol review fixes — regression tests
# ---------------------------------------------------------------------------
def test_error_outcome_leaves_no_permanent_marker_and_retries(session):
    """Defect #1: run_deep_decision never raises — a status='error' outcome is
    NON-completing, must NOT leave a permanent fired marker, and retries."""
    _write(session, "ERR", [{"kind": "dated_event", "date": "2026-01-01"}], 100)

    class _ErrOutcome:
        status = "error"
        blocked_by = "analysts_error"

    calls = {"n": 0}

    def decide_err(*, user_id, subject, cited_new_facts, reason):
        calls["n"] += 1
        return _ErrOutcome()

    s1 = sweep_and_fire(session, "ariel", now=NOW, decide_fn=decide_err)
    session.commit()
    assert s1["escalated"] == 0
    assert calls["n"] == 1
    assert _fired_markers(session) == []  # lease RELEASED, no permanent marker

    # Retries next sweep because the lease was released.
    ok = _RecordingDecide()
    s2 = sweep_and_fire(session, "ariel", now=NOW, decide_fn=ok)
    session.commit()
    assert s2["escalated"] == 1 and len(ok.calls) == 1


def test_blocked_by_real_decision_is_completing(session):
    """A genuine blocked-by-decision (e.g. verdict_defended / us_situs_floor) IS
    a completing re-verdict → keep the marker (do not retry forever)."""
    _write(session, "BLK", [{"kind": "dated_event", "date": "2026-01-01"}], 101)

    class _Blocked:
        status = "blocked"
        blocked_by = "verdict_defended"

    decide_calls = {"n": 0}

    def decide_blocked(*, user_id, subject, cited_new_facts, reason):
        decide_calls["n"] += 1
        return _Blocked()

    sweep_and_fire(session, "ariel", now=NOW, decide_fn=decide_blocked)
    session.commit()
    assert len(_fired_markers(session)) == 1
    # No re-fire next sweep.
    sweep_and_fire(session, "ariel", now=NOW, decide_fn=decide_blocked)
    session.commit()
    assert decide_calls["n"] == 1


def test_claim_collision_loser_skips_without_double_fire(session, monkeypatch):
    """Defect #2: two sweeps both pass the check-then-act pre-check → only ONE
    fires. Simulate by pre-inserting a live claim and forcing the pre-check to
    miss it; the loser's atomic INSERT collides, it skips, and the session is not
    poisoned (a second subject still fires + commit succeeds)."""
    _write(session, "RACE", [{"kind": "dated_event", "date": "2026-01-01"}], 110)
    _write(session, "SAFE", [{"kind": "dated_event", "date": "2026-01-01"}], 111)
    vid = _verdict_id(session, "RACE")

    # A concurrent sweep already holds the claim for RACE.
    session.add(
        ActionProposal(
            user_id="ariel",
            summary="concurrent claim",
            rationale_md="held by another sweep",
            suggested_payload="{}",
            severity="info",
            surfaced_at=NOW,
            expires_at=NOW + timedelta(days=1),
            status="open",
            kind="note_only",
            dedup_key=vt._fired_dedup_key("RACE", vid),
            execution_state="proposed",
        )
    )
    session.commit()

    # Force the fast-path pre-check to MISS so we exercise the atomic-claim path.
    monkeypatch.setattr(vt, "_already_fired", lambda *a, **k: False)

    decide = _RecordingDecide()
    summary = sweep_and_fire(session, "ariel", now=NOW, decide_fn=decide)
    session.commit()  # session must be usable despite the RACE IntegrityError

    fired_subjects = [c["subject"] for c in decide.calls]
    assert "RACE" not in fired_subjects  # loser never fired
    assert "SAFE" in fired_subjects  # winner still fired -> session not poisoned
    assert summary["escalated"] == 1


def test_malformed_price_quote_threshold_are_unevaluable(session):
    """Defect #3: non-numeric stored quote / threshold / metric -> UNEVALUABLE
    (never a raise that aborts the sweep, never a false trip). Other subjects
    still evaluate."""
    _write(session, "BADTHR", [{"kind": "price_below", "price": "abc"}], 120)
    _write(session, "BADQUOTE", [{"kind": "price_below", "price": 100.0}], 121)
    _write(
        session, "BADMETRIC",
        [{"kind": "metric_condition", "metric": "fcf", "op": "<", "value": "oops"}],
        122,
    )
    _write(session, "GOOD", [{"kind": "dated_event", "date": "2026-01-01"}], 123)

    quotes = {"BADTHR": 10.0, "BADQUOTE": "not-a-number"}
    results = evaluate_standing_verdict_triggers(
        session, "ariel", now=NOW,
        quote_fn=lambda s: quotes.get(s),
        macro_fn=lambda subj, metric: 5.0,
    )
    by = {r.subject: r for r in results}
    assert by["BADTHR"].unevaluable and not by["BADTHR"].tripped
    assert by["BADQUOTE"].unevaluable and not by["BADQUOTE"].tripped
    assert by["BADMETRIC"].unevaluable and not by["BADMETRIC"].tripped
    assert by["GOOD"].tripped  # sweep continued, valid subject still tripped


def test_date_with_trailing_garbage_is_unevaluable(session):
    """Defect #3: '2026-08-10garbage' must NOT be sliced-and-tripped."""
    _write(session, "GARB", [{"kind": "dated_event", "date": "2026-08-10garbage"}], 130)
    [res] = evaluate_standing_verdict_triggers(session, "ariel", now=NOW)
    assert not res.tripped and len(res.unevaluable) == 1
    assert "unparseable" in res.unevaluable[0].reason


def test_unknown_operator_is_unevaluable_not_tripped(session):
    """Defect #3: op='INVALID' must NOT fall through to equality."""
    _write(
        session, "OP",
        [{"kind": "metric_condition", "metric": "x", "op": "INVALID", "value": 5}],
        131,
    )
    [res] = evaluate_standing_verdict_triggers(
        session, "ariel", now=NOW, macro_fn=lambda subj, metric: 5.0
    )
    assert not res.tripped and len(res.unevaluable) == 1
    assert "unknown operator" in res.unevaluable[0].reason


def test_dated_event_honors_now_date_across_tz_boundary(session):
    """Defect #4: 2026-08-10 00:30+03:00 is Aug 10 by now.date() and must TRIP
    an Aug-10 event (a UTC re-projection would wrongly read Aug 9)."""
    _write(session, "TZ", [{"kind": "dated_event", "date": "2026-08-10"}], 140)
    tz_now = datetime(2026, 8, 10, 0, 30, tzinfo=timezone(timedelta(hours=3)))
    [res] = evaluate_standing_verdict_triggers(session, "ariel", now=tz_now)
    assert res.tripped and not res.unevaluable
    # Sanity: the UTC instant is Aug 9 — proving now.date() (not UTC) is used.
    assert tz_now.astimezone(timezone.utc).date() == date(2026, 8, 9)


def test_session_construction_failure_is_caught(session):
    """Defect #5: a session/engine build failure returns a failure summary — the
    job never raises out of tick()."""
    from argosy.orchestrator.loops.verdict_trigger_sweep import (
        VerdictTriggerSweepLoop,
    )

    def _boom():
        raise RuntimeError("engine construction failed")

    loop = VerdictTriggerSweepLoop(
        enabled=True, user_id="ariel", session_factory=_boom
    )
    result = asyncio.run(loop.tick(now=lambda: NOW))
    assert isinstance(result, dict) and "error" in result
    assert "engine construction failed" in result["error"]
