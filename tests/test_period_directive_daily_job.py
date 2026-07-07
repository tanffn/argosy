"""The daily proactive money loop (period_directive_daily): deterministic triage
→ fleet compose (stubbed — never a live LLM here) → one-row inbox sink with
refresh-in-place + auto-supersede. Real-schema writes go through the migrated DB
(the fake-db lesson of migration 0077: a CHECK failure looks exactly like a
dedup collision to a sink that swallows IntegrityError)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.orm import Session

from argosy.services.allocation_author.flow import AuthorOutcome
from argosy.services.allocation_author.proposal import AllocationProposal, Buy
from argosy.services.jobs.period_directive_daily import (
    PeriodDirectiveDailyJob,
    period_directive_daily_metadata,
    run_period_directive_daily,
)


def _event(excess_usd: float) -> SimpleNamespace:
    return SimpleNamespace(excess_usd=excess_usd)


def _accepted(*buys: Buy) -> AuthorOutcome:
    return AuthorOutcome(
        status="accepted",
        proposal=AllocationProposal(
            cash_to_deploy=sum(b.amount_usd for b in buys),
            buys=list(buys),
            rationale="Fills the largest NVDA-decorrelated plan gaps.",
        ),
        attempts=1,
    )


def _open_directives(s: Session) -> list:
    return s.execute(sa.text(
        "SELECT id, status, summary, suggested_payload FROM action_proposals "
        "WHERE kind='allocate' AND dedup_key LIKE 'period_directive:%' "
        "ORDER BY id"
    )).fetchall()


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------


def test_triage_skip_below_threshold_is_a_quiet_success(alembic_engine_at_head):
    """No cash overage → no LLM, no proposal, triggered=False (a skipped day is
    a SUCCESS state, not an error)."""
    def _no_compose(db, *, user_id, excess_usd):  # pragma: no cover — must not fire
        raise AssertionError("compose must not run when triage says quiet")

    with Session(alembic_engine_at_head) as s:
        out = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: None, compose_fn=_no_compose,
        )
    assert out["triggered"] is False
    assert "below plan-target threshold" in out["reason"]
    assert out["proposal_id"] is None and out["superseded"] == []


def test_triage_skip_when_open_directive_still_accurate(alembic_engine_at_head):
    """An open directive whose cash figure is within ±10% of today's → nothing
    new to say; the fleet never fires and the existing row keeps the slot."""
    compose_calls: list[float] = []

    def _compose(db, *, user_id, excess_usd):
        compose_calls.append(excess_usd)
        return _accepted(Buy(symbol="EXUS", amount_usd=excess_usd))

    with Session(alembic_engine_at_head) as s:
        # Day 1: trigger → compose → sink.
        out1 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(100_000.0),
            compose_fn=_compose,
        )
        assert out1["triggered"] is True and out1["proposal_id"] is not None
        # Day 2: cash drifted 5% — inside the band → quiet skip, no compose.
        out2 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(105_000.0),
            compose_fn=_compose,
        )
    assert out2["triggered"] is False
    assert "still accurate" in out2["reason"]
    assert out2["proposal_id"] == out1["proposal_id"]
    assert compose_calls == [100_000.0]  # the fleet fired exactly once


# --------------------------------------------------------------------------
# Compose → sink happy path
# --------------------------------------------------------------------------


def test_trigger_compose_sink_happy_path(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        out = run_period_directive_daily(
            s, "ariel",
            detect_fn=lambda db, *, user_id: _event(171_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="EXUS", amount_usd=80_000.0, sleeve="Ex-US developed"),
                Buy(symbol="FUSA", amount_usd=60_000.0, sleeve="US quality"),
                Buy(symbol="DPYA", amount_usd=31_000.0, sleeve="EM dividend"),
            ),
        )
        rows = _open_directives(s)
    assert out["triggered"] is True and out["cash_usd"] == 171_000.0
    assert out["proposal_id"] == rows[0][0]
    assert len(rows) == 1 and rows[0][1] == "open"
    summary = rows[0][2]
    # "Deploy ~$X idle cash: <top 3 buys> — full plan in the deploy tool"
    assert summary.startswith("Deploy ~$171k idle cash:")
    assert "EXUS $80k" in summary and "FUSA $60k" in summary and "DPYA $31k" in summary
    assert "full plan in the deploy tool" in summary
    payload = json.loads(rows[0][3])
    assert payload["excess_usd"] == 171_000.0
    assert [b["symbol"] for b in payload["buys"]] == ["EXUS", "FUSA", "DPYA"]


def test_degraded_author_writes_nothing(alembic_engine_at_head):
    """Author unavailable → degraded summary, NO proposal, NO deterministic
    fallback allocation — fail quiet-but-logged, retry next day."""
    with Session(alembic_engine_at_head) as s:
        out = run_period_directive_daily(
            s, "ariel",
            detect_fn=lambda db, *, user_id: _event(150_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: AuthorOutcome(
                status="unavailable", proposal=None, attempts=2,
            ),
        )
        assert _open_directives(s) == []
    assert out["triggered"] is True and out.get("degraded") is True
    assert out["proposal_id"] is None
    assert "author unavailable" in out["reason"]


# --------------------------------------------------------------------------
# Supersede / refresh-in-place
# --------------------------------------------------------------------------


def test_stale_directive_is_refreshed_in_place(alembic_engine_at_head):
    """Cash moved >10% → re-author; the dedup collision refreshes the OPEN row
    in place (same id) so the inbox never shows a stale amount."""
    with Session(alembic_engine_at_head) as s:
        out1 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(100_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="EXUS", amount_usd=excess_usd)),
        )
        out2 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(150_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="DPYA", amount_usd=excess_usd)),
        )
        rows = _open_directives(s)
    assert out2["triggered"] is True
    assert out2["proposal_id"] == out1["proposal_id"]  # same slot, refreshed
    assert len(rows) == 1
    assert "$150k" in rows[0][2] and "DPYA" in rows[0][2]
    assert "$100k" not in rows[0][2]


def test_open_directive_superseded_when_cash_falls_below_threshold(
    alembic_engine_at_head,
):
    """The cash got deployed / dropped under threshold → the standing directive
    is moot and must DISAPPEAR from the client's checklist (auto-supersede)."""
    with Session(alembic_engine_at_head) as s:
        out1 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(120_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="EXUS", amount_usd=excess_usd)),
        )
        out2 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: None,
            compose_fn=lambda db, *, user_id, excess_usd: None,
        )
        row = s.execute(sa.text(
            "SELECT status FROM action_proposals WHERE id = :i"
        ), {"i": out1["proposal_id"]}).fetchone()
    assert out2["triggered"] is False
    assert out2["superseded"] == [out1["proposal_id"]]
    assert row[0] == "superseded"


# --------------------------------------------------------------------------
# Scheduler contract
# --------------------------------------------------------------------------


def test_tick_accepts_the_scheduler_clock_keyword() -> None:
    """The scheduler calls every loop as tick(now=self.clock) — the keyword MUST
    be accepted (the pending_reevaluation_daily regression)."""
    captured = {}

    def _run(session, user_id):
        captured["user_id"] = user_id
        return {"triggered": False, "reason": "stub", "cash_usd": None,
                "proposal_id": None, "superseded": []}

    class _FakeSession:
        def close(self): pass

    job = PeriodDirectiveDailyJob(
        enabled=True, user_id="ariel",
        session_factory=lambda: _FakeSession(), run_fn=_run,
    )
    clock = lambda: datetime(2026, 7, 6, 16, 0, tzinfo=timezone.utc)  # noqa: E731
    out = asyncio.run(job.tick(now=clock))
    assert captured["user_id"] == "ariel"
    assert out["triggered"] is False
    assert job.last_output_summary == out


def test_period_directive_daily_registers_with_its_own_metadata() -> None:
    """Pin the real registration pair main.py uses: a CadenceLoop MUST register
    with long_running=False (the LongRunningJob-type discriminator — True makes
    JobRegistry.register raise and leaves the loop unrunnable)."""
    from argosy.services.jobs.registry import JobRegistry

    reg = JobRegistry()
    reg.register(job=PeriodDirectiveDailyJob(enabled=True, user_id="ariel"),
                 metadata=period_directive_daily_metadata())
    assert "period_directive_daily" in reg._jobs  # type: ignore[attr-defined]
    md = period_directive_daily_metadata()
    assert md.schedule_cron == "0 19 * * *" and md.long_running is False
