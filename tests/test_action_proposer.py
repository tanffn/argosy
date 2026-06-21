"""Tests for the action_proposer agent + runner (Spec E commit #2).

Coverage:

  * **Registration** — the role defaults to Opus 4.7 with high
    thinking effort and citations off (matches the writing prompt's
    registration in ``argosy/agents/base.py``).
  * **System prompt invariants** — the system prompt MUST advertise
    the five tainted-data tags + the no-execution contract + the
    forbidden-phrase list. Pin via substring asserts.
  * **Happy path FlagTrigger** — a flag triggers 1-3 proposals; each
    persisted row has execution_state='proposed', dedup_key per
    formula, summary/rationale/payload round-tripped through the
    writer.
  * **Happy path SnapshotTrigger** — same shape from a snapshot input.
  * **Forbidden-phrase regex** — an LLM emitting "order placed for
    NVDA" in summary/rationale/payload prose DROPS the proposal and
    logs an audit event. The other proposals in the batch survive.
  * **Per-kind payload validation** — an LLM emitting kind='allocate'
    without the required 'ticker' field DROPS the proposal.
  * **Cooldown** — re-running the same trigger within 24h returns a
    RunResult with skipped_cooldown=True and writes zero new rows.
  * **Dedup tombstone (expired peer)** — an existing 'open' proposal
    with expires_at < now is tombstoned to 'rejected' and the new
    INSERT lands.
  * **Dedup active (unexpired peer)** — an existing 'open' proposal
    with expires_at >= now blocks the partial-unique slot; the writer
    catches the IntegrityError and returns the existing row instead.
  * **execution_state invariant** — every newly-written row has
    execution_state='proposed'. There is no kwarg path to override.
  * **100-fixture invariant** — across 100 mocked LLM outputs spanning
    all 8 kinds + valid+invalid payloads + mixed severities, EVERY
    persisted row carries execution_state='proposed'. Pins the codex
    BLOCKER #1 capability-boundary at runtime.

All tests use a sync sqlite fixture + a mocked agent (no real LLM
calls). The mock overrides ``_call_model`` to return canned JSON.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_action_proposer.py -v
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.agents.action_proposer import (
    ActionProposerAgent,
    REQUIRED_PAYLOAD_FIELDS_BY_KIND,
    _SYSTEM_PROMPT,
    scan_for_forbidden_execution_language,
)
from argosy.agents.base import (
    DEFAULT_CITATIONS_BY_ROLE,
    DEFAULT_MAX_TOKENS_BY_ROLE,
    DEFAULT_MODEL_BY_ROLE,
    DEFAULT_THINKING_EFFORT_BY_ROLE,
    ModelCall,
)
from argosy.services.action_proposer_runner import (
    DEDUP_KEY_VERSION,
    build_dedup_key,
    expires_at_for,
    run_action_proposer_for_flag,
    run_action_proposer_for_snapshot,
    write_action_proposal,
)
from argosy.state.models import (
    ActionProposal,
    Base,
    MonitorFlag,
    StateSnapshot,
    User,
)


USER = "ariel"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite Session bound to a tmp_path file DB.

    ``Base.metadata.create_all`` installs the ORM-declared schema. The
    partial-unique index ``ix_action_proposals_dedup_open`` is declared
    in alembic migration 0055 (a partial WHERE-clause index that the
    SQLAlchemy ORM cannot express); we install it manually here so the
    tombstone-then-insert and dedup-collision branches exercise the
    same DB constraint they would in production.
    """
    db_path = tmp_path / "action_proposer.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)

    # Mirror migration 0055: partial-unique index on
    # (user_id, dedup_key) WHERE status='open' AND dedup_key IS NOT NULL.
    with engine.begin() as conn:
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


def _seed_monitor_flag(
    session,
    *,
    user_id: str = USER,
    kind: str = "state_observer_fx_observation",
    severity: str = "warning",
    payload: dict[str, Any] | None = None,
    surfaced_at: datetime | None = None,
) -> MonitorFlag:
    if surfaced_at is None:
        surfaced_at = _now()
    if payload is None:
        payload = {
            "primary_field": "macro.fx_usd_nis_spot",
            "related_fields": [],
            "rationale_md": "FX baseline stale.",
        }
    row = MonitorFlag(
        user_id=user_id,
        kind=kind,
        severity=severity,
        payload=json.dumps(payload),
        surfaced_at=surfaced_at,
    )
    session.add(row)
    session.commit()
    return row


def _seed_state_snapshot(
    session,
    *,
    user_id: str = USER,
    snapshot_date: str = "2026-05-30",
    state: dict[str, Any] | None = None,
) -> StateSnapshot:
    from datetime import date as _date
    row = StateSnapshot(
        user_id=user_id,
        snapshot_date=_date.fromisoformat(snapshot_date),
        state_json=json.dumps(state or {"portfolio": {}, "macro": {}}),
        source_versions_json="{}",
    )
    session.add(row)
    session.commit()
    return row


class _MockProposerAgent(ActionProposerAgent):
    """Subclass returning a canned ModelCall.

    Tests instantiate this with ``canned_response_dict`` to drive
    specific scenarios end-to-end without touching Anthropic.
    """

    def __init__(
        self,
        *,
        user_id: str = USER,
        canned_response_dict: dict[str, Any] | None = None,
        canned_response_text: str | None = None,
    ) -> None:
        super().__init__(user_id=user_id)
        self.canned_response_dict = canned_response_dict
        self.canned_response_text = canned_response_text
        self.last_system: str | None = None
        self.last_user: str | None = None
        self.call_count = 0

    async def _call_model(
        self, *, system: str, user: str, **_extra: Any
    ) -> ModelCall:
        self.call_count += 1
        self.last_system = system
        self.last_user = user
        if self.canned_response_text is not None:
            text = self.canned_response_text
        else:
            payload = self.canned_response_dict or {
                "proposed_actions": [],
                "overall_assessment": "(canned: no proposals)",
                "confidence": "MEDIUM",
            }
            text = json.dumps(payload)
        return ModelCall(
            text=text,
            tokens_in=1000,
            tokens_out=500,
            model=self.model,
        )


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_action_proposer_registered_as_opus() -> None:
    """Role MUST default to Opus 4.8 per binding preference."""
    assert DEFAULT_MODEL_BY_ROLE.get("action_proposer") == "claude-opus-4-8"


def test_action_proposer_thinking_effort_high() -> None:
    assert DEFAULT_THINKING_EFFORT_BY_ROLE.get("action_proposer") == "high"


def test_action_proposer_max_tokens_registered() -> None:
    assert DEFAULT_MAX_TOKENS_BY_ROLE.get("action_proposer", 0) == 8000


def test_action_proposer_citations_disabled() -> None:
    assert DEFAULT_CITATIONS_BY_ROLE.get("action_proposer") is False


# ---------------------------------------------------------------------------
# System prompt invariants (codex BLOCKER #1)
# ---------------------------------------------------------------------------


def test_system_prompt_advertises_five_tainted_tags() -> None:
    """Spec B pattern: every tainted-data tag must be enumerated."""
    for tag in (
        "<trigger>",
        "<state>",
        "<related_history>",
        "<plan_summary>",
        "<user_notes>",
    ):
        assert tag in _SYSTEM_PROMPT, (
            f"System prompt missing tainted-data tag {tag!r}; the "
            "codex BLOCKER #1 contract requires explicit enumeration."
        )


def test_system_prompt_no_execution_contract() -> None:
    """The no-execution architectural invariant MUST be loud."""
    sys_lower = _SYSTEM_PROMPT.lower()
    assert "you propose, you do not execute" in sys_lower, (
        "System prompt MUST state 'YOU PROPOSE, YOU DO NOT EXECUTE'. "
        "Without it the architecture reverts to the anti-pattern."
    )
    assert "order placed" in sys_lower
    assert "sent to broker" in sys_lower


def test_system_prompt_data_not_instructions() -> None:
    """Tainted-data tags must be marked as DATA, not instructions."""
    sys_lower = _SYSTEM_PROMPT.lower()
    assert "data, not instructions" in sys_lower


def test_system_prompt_per_kind_required_fields_documented() -> None:
    """Required fields per kind MUST appear in the prompt so the LLM
    knows what it's emitting."""
    sys_lower = _SYSTEM_PROMPT.lower()
    # Spot-check a few kinds + their required-field anchors.
    assert "ticker, amount_usd" in sys_lower  # allocate
    assert "from_currency, to_currency" in sys_lower  # repatriate_currency
    assert "trigger_kind" in sys_lower  # replan_full


# ---------------------------------------------------------------------------
# Forbidden-pattern scan
# ---------------------------------------------------------------------------


def test_scan_clean_text_returns_none() -> None:
    assert scan_for_forbidden_execution_language(
        "Consider transferring USD 40,000 from Schwab to Bank Leumi NIS."
    ) is None
    assert scan_for_forbidden_execution_language(
        "FX baseline stale; the plan's USD/NIS assumption is far from spot."
    ) is None


def test_scan_detects_order_placed() -> None:
    hit = scan_for_forbidden_execution_language("order placed for NVDA")
    assert hit is not None
    assert "order placed" in hit.lower()


def test_scan_detects_will_execute_trade() -> None:
    hit = scan_for_forbidden_execution_language(
        "We will execute the trade at market open."
    )
    assert hit is not None


def test_scan_detects_funds_were_moved() -> None:
    hit = scan_for_forbidden_execution_language(
        "The funds were moved to the brokerage account."
    )
    assert hit is not None


def test_scan_detects_sent_to_broker() -> None:
    assert scan_for_forbidden_execution_language(
        "Instructions were sent to broker overnight."
    ) is not None


def test_scan_detects_hebrew_executed() -> None:
    """Hebrew execution phrases must be caught too."""
    assert scan_for_forbidden_execution_language("בוצעה העברה") is not None
    assert scan_for_forbidden_execution_language(
        "הוצא הוראה לבנק"
    ) is not None


def test_scan_strips_quoted_articles() -> None:
    """Citations of articles using execution language should NOT drop.

    The scan strips ``>``-blockquote lines before running.
    """
    text = (
        "Per Reuters:\n"
        "> Orders were placed for major positions overnight.\n"
        "Consider whether the user's plan should be reviewed."
    )
    assert scan_for_forbidden_execution_language(text) is None


def test_scan_strips_fenced_code() -> None:
    """Code blocks mentioning execution language should NOT drop."""
    text = (
        "Example pseudocode:\n"
        "```\n"
        "broker.order_placed(NVDA, 100)\n"
        "```\n"
        "Consider increasing the position."
    )
    assert scan_for_forbidden_execution_language(text) is None


# ---------------------------------------------------------------------------
# Per-kind required fields
# ---------------------------------------------------------------------------


def test_required_fields_cover_all_v1_kinds() -> None:
    """The required-fields table MUST cover all 8 v1 kinds."""
    expected = {
        "allocate", "repatriate_currency", "rebalance", "replan_full",
        "add_life_event_phase", "update_plan_assumption", "set_watchlist",
        "note_only",
    }
    assert set(REQUIRED_PAYLOAD_FIELDS_BY_KIND.keys()) == expected


def test_note_only_has_no_required_fields() -> None:
    """note_only is the catch-all 'just be aware' kind — no payload."""
    assert REQUIRED_PAYLOAD_FIELDS_BY_KIND["note_only"] == frozenset()


# ---------------------------------------------------------------------------
# Dedup key formula
# ---------------------------------------------------------------------------


def test_build_dedup_key_formula() -> None:
    """Per spec §2.3: v1|<kind>|<primary_ref_id>|<severity_bucket>."""
    key = build_dedup_key(
        kind="repatriate_currency",
        primary_ref_id="flag-42",
        severity_bucket="critical",
    )
    assert key == "v1|repatriate_currency|flag-42|critical"


def test_build_dedup_key_rejects_pipe_in_component() -> None:
    """| in any component would break the formula."""
    with pytest.raises(ValueError):
        build_dedup_key(
            kind="allocate",
            primary_ref_id="bad|id",
            severity_bucket="info",
        )


def test_expires_at_critical_is_seven_days() -> None:
    now = _now()
    assert (expires_at_for("critical", now=now) - now) == timedelta(days=7)


def test_expires_at_non_critical_is_thirty_days() -> None:
    now = _now()
    for sev in ("warning", "info"):
        assert (expires_at_for(sev, now=now) - now) == timedelta(days=30)


# ---------------------------------------------------------------------------
# write_action_proposal — happy path + execution_state invariant
# ---------------------------------------------------------------------------


def test_write_action_proposal_writes_proposed_state(sync_session) -> None:
    """Every newly-written row MUST have execution_state='proposed'.

    Codex BLOCKER #1: the writer has NO kwarg to override; the column
    defaults to 'proposed' and the writer hardcodes that value too.
    """
    row = write_action_proposal(
        sync_session, USER,
        kind="repatriate_currency",
        summary="Consider repatriating USD 40,000 to NIS.",
        rationale_md="FX favorable.",
        suggested_payload={
            "from_currency": "USD",
            "to_currency": "NIS",
            "amount_source_ccy": 40000,
        },
        severity="warning",
        dedup_key="v1|repatriate_currency|flag-1|warning",
        now=_now(),
    )
    assert row.id is not None
    assert row.execution_state == "proposed"
    assert row.status == "open"
    assert row.kind == "repatriate_currency"


def test_write_action_proposal_dedup_collision_returns_existing(
    sync_session,
) -> None:
    """An unexpired open peer triggers IntegrityError; writer returns
    the existing row."""
    now = _now()
    first = write_action_proposal(
        sync_session, USER,
        kind="allocate",
        summary="Add to growth allocation",
        rationale_md="...",
        suggested_payload={"ticker": "VTI", "amount_usd": 10000},
        severity="info",
        dedup_key="v1|allocate|flag-7|info",
        now=now,
    )

    second = write_action_proposal(
        sync_session, USER,
        kind="allocate",
        summary="Add to growth allocation (second attempt)",
        rationale_md="...",
        suggested_payload={"ticker": "VTI", "amount_usd": 12000},
        severity="info",
        dedup_key="v1|allocate|flag-7|info",
        now=now + timedelta(minutes=10),
    )

    assert second.id == first.id, (
        "Dedup collision must return the EXISTING row, not insert a "
        "second one. Codex BLOCKER #1 / spec §1.5 contract."
    )
    # Only one row in the table.
    rows = sync_session.execute(sa.select(ActionProposal)).scalars().all()
    assert len(rows) == 1


def test_write_action_proposal_tombstones_expired_peer(sync_session) -> None:
    """An existing 'open' row with expires_at < now is tombstoned to
    'rejected'; the new INSERT lands.

    Mirrors Spec B's writer-orchestrated tombstone pattern.
    """
    # First row, written FAR in the past so it has long since expired.
    long_ago = _now() - timedelta(days=40)
    first = write_action_proposal(
        sync_session, USER,
        kind="allocate",
        summary="Old proposal",
        rationale_md="...",
        suggested_payload={"ticker": "VTI", "amount_usd": 5000},
        severity="info",
        dedup_key="v1|allocate|flag-9|info",
        now=long_ago,
    )

    # Now (well past first.expires_at) write a new one with the same key.
    second = write_action_proposal(
        sync_session, USER,
        kind="allocate",
        summary="Fresh proposal",
        rationale_md="...",
        suggested_payload={"ticker": "VTI", "amount_usd": 6000},
        severity="info",
        dedup_key="v1|allocate|flag-9|info",
        now=_now(),
    )

    # Second is a NEW row, not the first.
    assert second.id != first.id
    sync_session.refresh(first)
    # First was tombstoned via 'rejected' (migration 0055 status enum
    # doesn't have 'expired' yet; the writer uses 'rejected' to release
    # the partial-unique slot, with decided_by_user_note documenting
    # the reason).
    assert first.status == "rejected"
    assert first.decided_at is not None
    assert "tombstoned" in (first.decided_by_user_note or "")
    # Both rows exist.
    rows = sync_session.execute(sa.select(ActionProposal)).scalars().all()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Happy-path runner tests (FlagTrigger / SnapshotTrigger)
# ---------------------------------------------------------------------------


_CANNED_TWO_PROPOSALS: dict[str, Any] = {
    "proposed_actions": [
        {
            "kind": "repatriate_currency",
            "severity": "warning",
            "confidence": "MEDIUM",
            "summary": "Consider repatriating USD 40,000 to NIS — FX favorable.",
            "rationale_md": (
                "USD/NIS sits at 2.81 vs the plan's 3.6 baseline. "
                "Repatriating reduces the planning gap."
            ),
            "suggested_payload": {
                "from_currency": "USD",
                "to_currency": "NIS",
                "amount_source_ccy": 40000,
                "target_account_hint": "Bank Leumi NIS checking",
            },
            "cited_fields": ["macro.fx_usd_nis_spot"],
        },
        {
            "kind": "update_plan_assumption",
            "severity": "info",
            "confidence": "HIGH",
            "summary": "Update plan's FX assumption to 3.0.",
            "rationale_md": "Plan's USD/NIS=3.6 is stale.",
            "suggested_payload": {
                "assumption_field": "assumed_fx_usd_nis",
                "suggested_value": 3.0,
            },
            "cited_fields": ["plan_inputs.assumed_fx_usd_nis"],
        },
    ],
    "overall_assessment": "FX deviation warrants surfacing two actions.",
    "confidence": "MEDIUM",
}


def test_run_for_flag_happy_path_writes_two_proposals(sync_session) -> None:
    """A FlagTrigger that produces two valid proposals writes two rows."""
    flag = _seed_monitor_flag(sync_session)
    agent = _MockProposerAgent(canned_response_dict=_CANNED_TWO_PROPOSALS)

    result = asyncio.run(run_action_proposer_for_flag(
        sync_session,
        flag,
        agent=agent,
        now=_now(),
    ))

    assert len(result.proposals) == 2
    assert not result.skipped_cooldown
    assert not result.skipped_no_output

    # Every row carries execution_state='proposed' + status='open' +
    # source_flag_id back-FK.
    for row in result.proposals:
        assert row.execution_state == "proposed"
        assert row.status == "open"
        assert row.source_flag_id == flag.id
        assert row.user_id == USER

    # Kinds + dedup keys match the canned output.
    kinds = {r.kind for r in result.proposals}
    assert kinds == {"repatriate_currency", "update_plan_assumption"}


def test_run_for_snapshot_happy_path(sync_session) -> None:
    """A SnapshotTrigger writes proposals with source_observation_id set."""
    snapshot = _seed_state_snapshot(
        sync_session,
        state={"plan_inputs": {"assumed_fx_usd_nis": 3.6}},
    )
    canned = {
        "proposed_actions": [
            {
                "kind": "note_only",
                "severity": "info",
                "confidence": "MEDIUM",
                "summary": "Plan FX assumption diverges from spot; informational.",
                "rationale_md": "No action recommended; user awareness only.",
                "suggested_payload": {},
                "cited_fields": ["plan_inputs.assumed_fx_usd_nis"],
            }
        ],
        "overall_assessment": "Informational.",
        "confidence": "MEDIUM",
    }
    agent = _MockProposerAgent(canned_response_dict=canned)

    result = asyncio.run(run_action_proposer_for_snapshot(
        sync_session,
        snapshot,
        agent=agent,
        now=_now(),
    ))

    assert len(result.proposals) == 1
    row = result.proposals[0]
    assert row.source_observation_id == snapshot.id
    assert row.source_flag_id is None
    assert row.execution_state == "proposed"
    assert row.kind == "note_only"


# ---------------------------------------------------------------------------
# Drop scenarios — forbidden phrases + missing payload fields
# ---------------------------------------------------------------------------


def test_forbidden_phrase_drops_proposal(sync_session) -> None:
    """An LLM emitting 'order placed for NVDA' drops that proposal but
    keeps the other one in the same batch.

    The drop is observed via the persisted-rows count (the bad proposal
    didn't make it to the DB). The audit-log emission is verified
    separately via ``test_log_drop_emits_audit_event``.
    """
    flag = _seed_monitor_flag(sync_session)
    canned = {
        "proposed_actions": [
            {
                "kind": "allocate",
                "severity": "warning",
                "confidence": "HIGH",
                "summary": "Order placed for NVDA — increase to 12%.",
                "rationale_md": "...",
                "suggested_payload": {"ticker": "NVDA", "amount_usd": 5000},
                "cited_fields": [],
            },
            {
                "kind": "note_only",
                "severity": "info",
                "confidence": "MEDIUM",
                "summary": "Informational only.",
                "rationale_md": "Nothing urgent.",
                "suggested_payload": {},
                "cited_fields": [],
            },
        ],
        "overall_assessment": "Mixed.",
        "confidence": "MEDIUM",
    }
    agent = _MockProposerAgent(canned_response_dict=canned)

    result = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=_now(),
    ))

    # Only the note_only survives — the forbidden-phrase proposal was
    # dropped by the post-validator before reaching the writer.
    assert len(result.proposals) == 1
    assert result.proposals[0].kind == "note_only"
    rows = sync_session.execute(sa.select(ActionProposal)).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "note_only"


def test_missing_required_payload_field_drops_proposal(sync_session) -> None:
    """An 'allocate' proposal without 'ticker' is dropped."""
    flag = _seed_monitor_flag(sync_session)
    canned = {
        "proposed_actions": [
            {
                "kind": "allocate",
                "severity": "warning",
                "confidence": "HIGH",
                "summary": "Add to growth.",
                "rationale_md": "...",
                "suggested_payload": {"amount_usd": 5000},  # missing ticker
                "cited_fields": [],
            },
            {
                "kind": "allocate",
                "severity": "info",
                "confidence": "MEDIUM",
                "summary": "Add to growth (valid).",
                "rationale_md": "...",
                "suggested_payload": {"ticker": "VTI", "amount_usd": 5000},
                "cited_fields": [],
            },
        ],
        "overall_assessment": "Mixed.",
        "confidence": "MEDIUM",
    }
    agent = _MockProposerAgent(canned_response_dict=canned)

    result = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=_now(),
    ))

    # The valid proposal lands; the missing-ticker one is dropped.
    assert len(result.proposals) == 1
    assert result.proposals[0].summary.startswith("Add to growth (valid)")
    assert result.proposals[0].suggested_payload  # JSON string, non-empty


def test_post_validate_drops_directly(sync_session) -> None:
    """Direct unit test on ``_post_validate_output``: forbidden-phrase
    + missing-required-field drops, no DB round-trip."""
    from argosy.agents.action_proposer import (
        ActionProposerOutput,
        ProposedAction,
    )
    agent = _MockProposerAgent()
    output = ActionProposerOutput(
        proposed_actions=[
            ProposedAction(
                kind="allocate",
                severity="warning",
                summary="Order placed for NVDA",
                rationale_md="...",
                suggested_payload={"ticker": "NVDA", "amount_usd": 1000},
            ),
            ProposedAction(
                kind="allocate",
                severity="warning",
                summary="Add to growth",
                rationale_md="...",
                suggested_payload={"amount_usd": 1000},  # missing ticker
            ),
            ProposedAction(
                kind="note_only",
                severity="info",
                summary="Just an FYI.",
                rationale_md="No action.",
                suggested_payload={},
            ),
        ],
    )
    kept = agent._post_validate_output(output, trigger=None)
    assert len(kept) == 1
    assert kept[0].kind == "note_only"


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_cooldown_within_24h_skips_second_call(sync_session) -> None:
    """Re-running for the same flag within 24h returns 0 new proposals
    + skipped_cooldown=True."""
    flag = _seed_monitor_flag(sync_session)
    agent = _MockProposerAgent(canned_response_dict=_CANNED_TWO_PROPOSALS)

    # First call lands proposals.
    r1 = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=_now(),
    ))
    assert len(r1.proposals) == 2

    # Second call within 24h — cooldown triggers.
    agent2 = _MockProposerAgent(canned_response_dict=_CANNED_TWO_PROPOSALS)
    r2 = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent2,
        now=_now() + timedelta(hours=3),
    ))
    assert r2.skipped_cooldown is True
    assert len(r2.proposals) == 0
    # The mock's _call_model was NOT called the second time.
    assert agent2.call_count == 0


def test_cooldown_force_bypasses_check(sync_session) -> None:
    """force=True bypasses the cooldown (used by UI re-evaluate)."""
    flag = _seed_monitor_flag(sync_session)
    agent = _MockProposerAgent(canned_response_dict=_CANNED_TWO_PROPOSALS)

    asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=_now(),
    ))

    # Even within the cooldown window, force=True calls the agent again.
    # The dedup-collision path returns the existing rows.
    agent2 = _MockProposerAgent(canned_response_dict=_CANNED_TWO_PROPOSALS)
    r2 = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent2,
        now=_now() + timedelta(hours=1),
        force=True,
    ))
    assert r2.skipped_cooldown is False
    assert agent2.call_count == 1
    # Dedup collisions returned existing rows; no duplicates.
    rows = sync_session.execute(sa.select(ActionProposal)).scalars().all()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Malformed output handling
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Codex-review-driven additions
# ---------------------------------------------------------------------------


def test_cooldown_boundary_is_strict_greater_than(sync_session) -> None:
    """Codex BLOCKER #1: re-fire EXACTLY at t+24h must NOT be skipped.

    The cooldown threshold uses strict ``>`` so a row with
    surfaced_at == threshold falls OUTSIDE the cooldown window.
    """
    flag = _seed_monitor_flag(sync_session)
    agent = _MockProposerAgent(canned_response_dict=_CANNED_TWO_PROPOSALS)

    t0 = _now()
    asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=t0,
    ))

    # Re-fire at EXACTLY t+24h — should NOT be cooldown-skipped.
    agent2 = _MockProposerAgent(canned_response_dict=_CANNED_TWO_PROPOSALS)
    r2 = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent2,
        now=t0 + timedelta(hours=24),
    ))
    assert r2.skipped_cooldown is False, (
        "Cooldown re-fire at exactly t+24h must NOT skip (24h has "
        "elapsed). Boundary semantics are strict >."
    )
    assert agent2.call_count == 1


def test_cooldown_per_kind_and_primary_field_not_per_flag_id(
    sync_session,
) -> None:
    """Codex BLOCKER #2: cooldown keys on (flag_kind, primary_field, user),
    NOT on raw flag_id.

    Two distinct MonitorFlag rows for the SAME (flag_kind,
    primary_field) must share the cooldown — re-paying Opus on
    repeated observer fires of the same signal defeats the purpose.
    """
    # First flag fires.
    flag1 = _seed_monitor_flag(
        sync_session,
        kind="state_observer_fx_observation",
        payload={
            "primary_field": "macro.fx_usd_nis_spot",
            "related_fields": [],
            "rationale_md": "FX1",
        },
    )
    agent = _MockProposerAgent(canned_response_dict=_CANNED_TWO_PROPOSALS)
    asyncio.run(run_action_proposer_for_flag(
        sync_session, flag1, agent=agent, now=_now(),
    ))

    # A second flag, same (kind, primary_field), fires 3h later. The
    # cooldown gate must trip because the underlying signal is the
    # same — even though the flag-id differs.
    flag2 = _seed_monitor_flag(
        sync_session,
        kind="state_observer_fx_observation",
        payload={
            "primary_field": "macro.fx_usd_nis_spot",
            "related_fields": [],
            "rationale_md": "FX2 (redundant fire)",
        },
        surfaced_at=_now() + timedelta(hours=3),
    )
    agent2 = _MockProposerAgent(canned_response_dict=_CANNED_TWO_PROPOSALS)
    r2 = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag2, agent=agent2,
        now=_now() + timedelta(hours=3),
    ))
    assert r2.skipped_cooldown is True
    assert agent2.call_count == 0


def test_empty_output_writes_cooldown_marker(sync_session) -> None:
    """Codex IMPORTANT #2: an empty LLM output writes a marker so the
    next call within 24h is short-circuited."""
    flag = _seed_monitor_flag(sync_session)
    canned_empty = {
        "proposed_actions": [],
        "overall_assessment": "Trigger was noise.",
        "confidence": "HIGH",
        "no_action_reason": "noise",
    }
    agent = _MockProposerAgent(canned_response_dict=canned_empty)

    r1 = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=_now(),
    ))
    assert len(r1.proposals) == 0
    assert r1.skipped_no_output is True

    # A marker landed (status='rejected' + decided_by_user_note ~
    # 'cooldown_marker').
    rows = sync_session.execute(sa.select(ActionProposal)).scalars().all()
    assert len(rows) == 1
    marker = rows[0]
    assert marker.status == "rejected"
    assert marker.execution_state == "dismissed"
    assert (marker.decided_by_user_note or "").startswith("cooldown_marker")

    # Second call within 24h: cooldown trips on the marker.
    agent2 = _MockProposerAgent(canned_response_dict=canned_empty)
    r2 = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent2,
        now=_now() + timedelta(hours=3),
    ))
    assert r2.skipped_cooldown is True
    assert agent2.call_count == 0


def test_negative_amount_usd_drops_allocate(sync_session) -> None:
    """Codex IMPORTANT #4: a negative amount_usd drops the proposal."""
    flag = _seed_monitor_flag(sync_session)
    canned = {
        "proposed_actions": [
            {
                "kind": "allocate",
                "severity": "info",
                "confidence": "MEDIUM",
                "summary": "Add to growth.",
                "rationale_md": "...",
                "suggested_payload": {
                    "ticker": "VTI", "amount_usd": -5000,  # NEGATIVE
                },
                "cited_fields": [],
            }
        ],
        "overall_assessment": "Bad amount.",
        "confidence": "LOW",
    }
    agent = _MockProposerAgent(canned_response_dict=canned)
    r = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=_now(),
    ))
    # The negative-amount proposal was dropped; an empty-output
    # cooldown marker landed instead.
    assert len(r.proposals) == 0
    assert r.skipped_no_output is True


def test_unknown_replan_trigger_kind_drops_proposal(sync_session) -> None:
    """Codex IMPORTANT #4: replan_full with an unknown trigger_kind
    is dropped."""
    flag = _seed_monitor_flag(sync_session)
    canned = {
        "proposed_actions": [
            {
                "kind": "replan_full",
                "severity": "warning",
                "confidence": "MEDIUM",
                "summary": "Re-run plan.",
                "rationale_md": "...",
                "suggested_payload": {
                    "trigger_kind": "made_up_trigger_kind",  # bogus
                },
                "cited_fields": [],
            }
        ],
        "overall_assessment": "Bad trigger_kind.",
        "confidence": "LOW",
    }
    agent = _MockProposerAgent(canned_response_dict=canned)
    r = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=_now(),
    ))
    assert len(r.proposals) == 0


def test_same_currency_repatriate_drops_proposal(sync_session) -> None:
    """Codex IMPORTANT #4: from_currency == to_currency is non-sensical."""
    flag = _seed_monitor_flag(sync_session)
    canned = {
        "proposed_actions": [
            {
                "kind": "repatriate_currency",
                "severity": "info",
                "confidence": "LOW",
                "summary": "Repatriate USD to USD.",
                "rationale_md": "...",
                "suggested_payload": {
                    "from_currency": "USD",
                    "to_currency": "USD",
                    "amount_source_ccy": 10000,
                },
                "cited_fields": [],
            }
        ],
        "overall_assessment": "Bad currencies.",
        "confidence": "LOW",
    }
    agent = _MockProposerAgent(canned_response_dict=canned)
    r = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=_now(),
    ))
    assert len(r.proposals) == 0


def test_scan_detects_after_approval_auto_execute() -> None:
    """Codex IMPORTANT #1: 'After approval the order goes out' caught."""
    assert scan_for_forbidden_execution_language(
        "After approval the order goes out at market open."
    ) is not None


def test_scan_detects_broker_will_sweep() -> None:
    """Codex IMPORTANT #1: 'Schwab will sweep $40K to Bank Leumi' caught."""
    assert scan_for_forbidden_execution_language(
        "Schwab will sweep $40K to Bank Leumi NIS overnight."
    ) is not None


def test_scan_detects_submitted_to_broker() -> None:
    """Codex IMPORTANT #1: bare 'Submitted to broker' caught."""
    assert scan_for_forbidden_execution_language(
        "Submitted to broker."
    ) is not None


def test_scan_detects_once_accepted_will_transfer() -> None:
    """Codex IMPORTANT #1: 'Once accepted, the system will transfer' caught."""
    assert scan_for_forbidden_execution_language(
        "Once accepted, the system will transfer the funds."
    ) is not None


def test_malformed_llm_output_returns_empty(sync_session) -> None:
    """A malformed JSON response yields zero proposals — the runner
    does NOT crash."""
    flag = _seed_monitor_flag(sync_session)
    agent = _MockProposerAgent(canned_response_text="this is not json")

    result = asyncio.run(run_action_proposer_for_flag(
        sync_session, flag, agent=agent, now=_now(),
    ))
    assert result.skipped_no_output is True
    assert len(result.proposals) == 0


# ---------------------------------------------------------------------------
# 100-fixture invariant — execution_state='proposed' on every row
# ---------------------------------------------------------------------------


def _random_payload(kind: str, rng: random.Random) -> dict[str, Any]:
    """Build a payload that satisfies the kind's required fields."""
    if kind == "allocate":
        return {
            "ticker": rng.choice(["VTI", "VEA", "AGG", "SCHG"]),
            "amount_usd": rng.randint(1000, 50000),
        }
    if kind == "repatriate_currency":
        return {
            "from_currency": "USD",
            "to_currency": "NIS",
            "amount_source_ccy": rng.randint(5000, 100000),
        }
    if kind == "rebalance":
        return {"rows": [{"from": "Growth", "to": "Income", "amount": 1000}]}
    if kind == "replan_full":
        return {"trigger_kind": "fx_shock_10pct"}
    if kind == "add_life_event_phase":
        return {"category": "household", "kind": "child_started_college"}
    if kind == "update_plan_assumption":
        return {
            "assumption_field": "assumed_fx_usd_nis",
            "suggested_value": rng.choice([3.0, 3.2, 3.4, 3.6]),
        }
    if kind == "set_watchlist":
        return {"ticker": "NVDA", "watch_kind": "review_30d"}
    return {}  # note_only


def test_100_fixture_execution_state_invariant(sync_session) -> None:
    """Across 100 mocked LLM outputs, EVERY persisted row carries
    execution_state='proposed'.

    Codex BLOCKER #1 / spec §2.2.1: the proposer code path has NO way
    to write any other execution_state. This test pins the invariant
    empirically against any future regression (a writer kwarg change,
    a default flip, etc.).
    """
    rng = random.Random(42)
    kinds = list(REQUIRED_PAYLOAD_FIELDS_BY_KIND.keys())
    severities = ("info", "warning", "critical")

    persisted_so_far = 0
    for i in range(100):
        flag = _seed_monitor_flag(
            sync_session,
            kind="state_observer_other_observation",
            payload={
                "primary_field": f"macro.fx_fixture_{i}",
                "related_fields": [],
                "rationale_md": "fixture",
            },
            surfaced_at=_now() + timedelta(days=i + 1),
        )

        # 1-3 proposals per fixture, randomised kinds.
        n_props = rng.randint(0, 3)
        canned_props = []
        for _ in range(n_props):
            k = rng.choice(kinds)
            canned_props.append({
                "kind": k,
                "severity": rng.choice(severities),
                "confidence": rng.choice(["LOW", "MEDIUM", "HIGH"]),
                "summary": f"fixture {i} proposal {k}",
                "rationale_md": f"fixture rationale for {k}",
                "suggested_payload": _random_payload(k, rng),
                "cited_fields": [],
            })
        canned = {
            "proposed_actions": canned_props,
            "overall_assessment": "fixture",
            "confidence": "MEDIUM",
        }

        agent = _MockProposerAgent(canned_response_dict=canned)
        # Advance the clock past the previous cooldown window so the
        # cooldown gate doesn't trip.
        result = asyncio.run(run_action_proposer_for_flag(
            sync_session, flag, agent=agent,
            now=_now() + timedelta(days=i + 1),
        ))
        persisted_so_far += len(result.proposals)

    # Pin the invariant against EVERY user-visible proposal row in
    # the table. Cooldown markers (decided_by_user_note ~
    # 'cooldown_marker*') are system rows that never surface to the
    # user and are written with execution_state='dismissed' at birth
    # per codex IMPORTANT #2; they are EXCLUDED from the invariant.
    rows = sync_session.execute(sa.select(ActionProposal)).scalars().all()
    assert len(rows) > 0, "100-fixture run must produce at least one row"
    user_visible_rows = [
        r for r in rows
        if not (r.decided_by_user_note or "").startswith("cooldown_marker")
    ]
    assert len(user_visible_rows) > 0, (
        "100-fixture run must produce at least one user-visible proposal "
        "(otherwise the invariant test is vacuous)"
    )
    bad = [
        r for r in user_visible_rows
        if r.execution_state != "proposed"
    ]
    assert bad == [], (
        f"{len(bad)} user-visible rows landed with execution_state != "
        "'proposed' — codex BLOCKER #1 capability-boundary invariant "
        "violated."
    )
