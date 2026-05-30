"""Tests for the state observer → action proposer wiring (Spec E).

Coverage:

  * **Wiring fires** — when the state observer flag writer commits a
    severity>=warning row, ``run_action_proposer_for_flag`` runs against
    it and creates >= 1 ``action_proposals`` row.
  * **Info severity skipped** — the proposer is NOT invoked for info-
    band flags (spec §2.5 gate).
  * **Per-flag try/except** — a proposer failure on one flag does not
    break the rest of the batch (the flag row itself still lands).
  * **Cooldown honored** — refiring the same (flag_kind, primary_field)
    inside the 24h window short-circuits the proposer (no second row).

The LLM call is mocked via the same ``_MockProposerAgent`` pattern as
``test_action_proposer.py`` — we monkeypatch
``argosy.services.action_proposer_runner.ActionProposerAgent`` so the
runner's default-agent path instantiates the mock instead of touching
Anthropic.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_state_observer_proposer_wired.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.action_proposer import ActionProposerAgent
from argosy.agents.base import ConfidenceBand, ModelCall
from argosy.agents.state_observer import FlagCandidate
from argosy.services import action_proposer_runner
from argosy.services.state_observer_flag_writer import write_observer_flags
from argosy.state.models import (
    ActionProposal,
    Base,
    MonitorFlag,
    User,
)


USER = "ariel"
SNAPSHOT_ID = 17


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite Session bound to a tmp_path file DB.

    Installs BOTH partial-unique indexes (observer's dedup_key + the
    proposer's dedup_open) so the end-to-end chain exercises the same
    constraints as production.
    """
    db_path = tmp_path / "observer_proposer_wired.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        # migration 0049 — observer flags dedup
        conn.execute(sa.text(
            "CREATE UNIQUE INDEX ix_monitor_flags_observer_dedup "
            "ON monitor_flags (user_id, dedup_key) "
            "WHERE dedup_key IS NOT NULL AND acknowledged_at IS NULL"
        ))
        # migration 0055 — action_proposals dedup
        conn.execute(sa.text(
            "CREATE UNIQUE INDEX ix_action_proposals_dedup_open "
            "ON action_proposals (user_id, dedup_key) "
            "WHERE status = 'open' AND dedup_key IS NOT NULL"
        ))

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    db.add(User(id=USER, plan="free"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _now() -> datetime:
    return datetime(2026, 5, 30, 17, 0, 0, tzinfo=timezone.utc)


def _make_candidate(
    *,
    primary_field: str = "macro.fx_usd_nis_spot",
    severity: str = "warning",
    inferred_kind: str = "fx_observation",
    deviation_bucket: str = "large",
    rationale_md: str = "Plan baseline drift.",
) -> FlagCandidate:
    return FlagCandidate(
        severity=severity,  # type: ignore[arg-type]
        primary_field=primary_field,
        related_fields=[],
        rationale_md=rationale_md,
        inferred_kind=inferred_kind,
        deviation_bucket=deviation_bucket,  # type: ignore[arg-type]
        mitigation_hint=None,
        confidence=ConfidenceBand.HIGH,
        validator_actions=[],
    )


_CANNED_ONE_PROPOSAL: dict[str, Any] = {
    "proposed_actions": [
        {
            "kind": "repatriate_currency",
            "severity": "warning",
            "confidence": "MEDIUM",
            "summary": "Consider repatriating USD 40,000 to NIS — FX favorable.",
            "rationale_md": (
                "USD/NIS sits at 2.81 vs plan baseline 3.6.  "
                "Repatriating reduces the planning gap."
            ),
            "suggested_payload": {
                "from_currency": "USD",
                "to_currency": "NIS",
                "amount_source_ccy": 40000,
            },
            "cited_fields": ["macro.fx_usd_nis_spot"],
        },
    ],
    "overall_assessment": "FX deviation warrants surfacing one action.",
    "confidence": "MEDIUM",
}


class _MockProposerAgent(ActionProposerAgent):
    """Subclass that returns a canned LLM response.

    Mirrors the pattern in ``tests/test_action_proposer.py``; we
    monkeypatch ``ActionProposerAgent`` in the runner module so the
    runner's default-agent construction picks this up.
    """

    canned_response_dict: dict[str, Any] = _CANNED_ONE_PROPOSAL
    call_count_per_class: int = 0

    async def _call_model(
        self, *, system: str, user: str, **_extra: Any
    ) -> ModelCall:
        type(self).call_count_per_class += 1
        return ModelCall(
            text=json.dumps(self.canned_response_dict),
            tokens_in=1000,
            tokens_out=500,
            model=self.model,
        )


class _EmptyProposerAgent(ActionProposerAgent):
    """Mock that returns zero proposals — the cooldown-marker path."""

    call_count_per_class: int = 0

    async def _call_model(
        self, *, system: str, user: str, **_extra: Any
    ) -> ModelCall:
        type(self).call_count_per_class += 1
        return ModelCall(
            text=json.dumps({
                "proposed_actions": [],
                "overall_assessment": "(noise)",
                "confidence": "MEDIUM",
                "no_action_reason": "trigger was noise",
            }),
            tokens_in=100,
            tokens_out=50,
            model=self.model,
        )


@pytest.fixture
def patch_proposer_agent(monkeypatch):
    """Monkeypatch the runner's ``ActionProposerAgent`` to the mock.

    The flag writer late-imports ``run_action_proposer_for_flag`` from
    ``argosy.services.action_proposer_runner``.  That function reads
    ``ActionProposerAgent`` from the same module namespace — replacing
    it here makes the runner's default-agent construction return our
    mock instead of touching Anthropic.
    """
    _MockProposerAgent.call_count_per_class = 0
    monkeypatch.setattr(
        action_proposer_runner, "ActionProposerAgent", _MockProposerAgent,
    )
    yield _MockProposerAgent


# ---------------------------------------------------------------------------
# Wiring tests
# ---------------------------------------------------------------------------


def test_wired_flag_writer_invokes_proposer_for_warning(
    sync_session, patch_proposer_agent,
) -> None:
    """A warning-severity flag write fires the proposer + persists >= 1
    action_proposals row.

    This is the central regression test for the facade-audit finding:
    before this wiring, ``run_action_proposer_for_flag`` had zero
    production callers so flags surfaced but no proposals materialised.
    """
    summary = write_observer_flags(
        sync_session,
        USER,
        [_make_candidate(severity="warning")],
        snapshot_id=SNAPSHOT_ID,
        now=_now(),
    )

    assert summary.written_count == 1
    assert summary.errors == []

    # The mock LLM was invoked exactly once.
    assert patch_proposer_agent.call_count_per_class == 1

    # And at least one action_proposals row landed, FK'd back to the
    # monitor_flags row.
    flag_row = sync_session.execute(sa.select(MonitorFlag)).scalar_one()
    proposals = sync_session.execute(
        sa.select(ActionProposal).order_by(ActionProposal.id)
    ).scalars().all()
    assert len(proposals) >= 1, (
        "the wiring is broken: a warning flag was written but the "
        "proposer did not produce any action_proposals rows"
    )
    surfaced = proposals[0]
    assert surfaced.source_flag_id == flag_row.id
    assert surfaced.kind == "repatriate_currency"
    assert surfaced.severity == "warning"
    assert surfaced.status == "open"
    assert surfaced.execution_state == "proposed"
    assert surfaced.user_id == USER


def test_wired_flag_writer_skips_proposer_for_info(
    sync_session, patch_proposer_agent,
) -> None:
    """Info-band flag must NOT trigger the proposer (spec §2.5 gate)."""
    summary = write_observer_flags(
        sync_session,
        USER,
        [_make_candidate(severity="info")],
        snapshot_id=SNAPSHOT_ID,
        now=_now(),
    )
    assert summary.written_count == 1
    # Proposer was NOT invoked.
    assert patch_proposer_agent.call_count_per_class == 0
    # No action_proposals rows.
    proposals = sync_session.execute(
        sa.select(ActionProposal)
    ).scalars().all()
    assert proposals == []


def test_wired_proposer_failure_does_not_break_flag_batch(
    sync_session, monkeypatch,
) -> None:
    """A proposer crash on ONE flag must not roll back the flag write
    or break the batch.

    We force ``run_action_proposer_for_flag`` to raise; the per-flag
    try/except in the flag writer must swallow + log + continue.
    """
    crash_calls: list[Any] = []

    async def _crashing_runner(session, monitor_flag, **_kwargs):
        crash_calls.append(monitor_flag.id)
        raise RuntimeError("simulated proposer crash")

    monkeypatch.setattr(
        action_proposer_runner,
        "run_action_proposer_for_flag",
        _crashing_runner,
    )

    summary = write_observer_flags(
        sync_session,
        USER,
        [_make_candidate(severity="critical")],
        snapshot_id=SNAPSHOT_ID,
        now=_now(),
    )

    # Flag write succeeded despite the proposer crash.
    assert summary.written_count == 1
    assert summary.errors == []
    # And the proposer WAS called (the wiring fired; the crash was
    # caught in the safe wrapper).
    assert len(crash_calls) == 1

    # The flag row is committed + visible.
    flag_row = sync_session.execute(sa.select(MonitorFlag)).scalar_one()
    assert flag_row.severity == "critical"

    # And no action_proposals row (the crash prevented one).
    proposals = sync_session.execute(
        sa.select(ActionProposal)
    ).scalars().all()
    assert proposals == []


def test_wired_proposer_cooldown_dedup_blocks_second_run(
    sync_session, patch_proposer_agent,
) -> None:
    """Refiring the same (flag_kind, primary_field) inside the 24h
    cooldown window short-circuits the second proposer call.

    Asserts the proposer runner's existing cooldown helper is honored
    by the wired path — not a freshly-added gate, just confirming the
    chain DOESN'T re-pay Opus per re-fire.
    """
    # First run — fires the proposer and writes a proposal.
    write_observer_flags(
        sync_session,
        USER,
        [_make_candidate(severity="warning")],
        snapshot_id=SNAPSHOT_ID,
        now=_now(),
    )
    assert patch_proposer_agent.call_count_per_class == 1
    proposals_after_first = sync_session.execute(
        sa.select(ActionProposal)
    ).scalars().all()
    assert len(proposals_after_first) == 1

    # User acknowledges the flag so a fresh write isn't deduplicated at
    # the flag layer (we want to specifically exercise the PROPOSER
    # layer's cooldown).
    flag_row = sync_session.execute(sa.select(MonitorFlag)).scalar_one()
    flag_row.acknowledged_at = _now() + timedelta(minutes=1)
    sync_session.commit()

    # Move the dedup_key (bucket change) so the SECOND flag DOES land
    # despite the §4.3 branch (c) acknowledged-peer rule.
    second_cand = _make_candidate(
        severity="warning",
        deviation_bucket="extreme",  # bucket move ⇒ fresh dedup_key
    )

    write_observer_flags(
        sync_session,
        USER,
        [second_cand],
        snapshot_id=SNAPSHOT_ID,
        # Same hour — inside the 24h proposer cooldown window.
        now=_now() + timedelta(hours=2),
    )

    # Two monitor_flags rows now exist (one acknowledged, one fresh).
    flags = sync_session.execute(sa.select(MonitorFlag)).scalars().all()
    assert len(flags) == 2

    # But the proposer was NOT invoked a second time — cooldown gate
    # short-circuits before the LLM call.
    assert patch_proposer_agent.call_count_per_class == 1, (
        "the proposer was called twice within the 24h cooldown "
        "window for the same (flag_kind, primary_field) — the wiring "
        "did not honor the runner's existing cooldown helper"
    )

    # And only one action_proposals row (the original).
    proposals_after_second = sync_session.execute(
        sa.select(ActionProposal).where(ActionProposal.status == "open")
    ).scalars().all()
    assert len(proposals_after_second) == 1
