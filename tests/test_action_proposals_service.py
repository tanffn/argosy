"""Tests for the action_proposals service (Spec E commit #6).

Coverage:

  * **list_open_action_proposals** — only ``status='open'`` rows; sort
    order is critical > warning > info, then ``surfaced_at`` desc.
  * **accept_action_proposal** happy path — plain Accept flips
    ``status='accepted'`` + ``execution_state='accepted_pending_user_
    action'`` + stamps ``decided_at``. The customize variant also
    persists the edited payload.
  * **defer_action_proposal** happy path — flips ``status='deferred'``
    + encodes ``defer_until_date`` into ``decided_by_user_note``.
    ``execution_state`` is INTENTIONALLY untouched (defer is not
    consent).
  * **reject_action_proposal** happy path — flips ``status='rejected'``
    + ``execution_state='dismissed'`` + records reason.
  * **no-execution invariant pin** (codex BLOCKER #1) — every code
    path through the service module ends with ``execution_state`` in
    ``{proposed, accepted_pending_user_action, dismissed}``; the
    Accept handler NEVER advances the column beyond
    ``accepted_pending_user_action``. The test exhaustively walks
    every transition.

Sync sqlite fixture pattern matched from test_action_proposer.py so
the partial-unique dedup index installs cleanly.

Test command:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_action_proposals_service.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.action_proposals import (
    InvalidProposalStateError,
    ProposalNotFoundError,
    accept_action_proposal,
    defer_action_proposal,
    list_open_action_proposals,
    reject_action_proposal,
    to_view,
)
from argosy.state.models import ActionProposal, Base, User


USER = "ariel"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_session(tmp_path):
    """Sync sqlite Session bound to a tmp_path file DB."""
    db_path = tmp_path / "action_proposals.db"
    engine = sa.create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)

    # Mirror migration 0055: partial-unique index on
    # (user_id, dedup_key) WHERE status='open' AND dedup_key IS NOT NULL.
    # Pattern from test_action_proposer.py — the ORM can't express
    # partial-WHERE indices so we install manually.
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


def _seed_proposal(
    session,
    *,
    user_id: str = USER,
    kind: str = "repatriate_currency",
    severity: str = "warning",
    status: str = "open",
    surfaced_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
    dedup_key: str | None = None,
) -> ActionProposal:
    """Insert one ActionProposal row directly (bypasses the runner).

    Tests want fine control over status / severity / surfaced_at to
    exercise specific service paths; the runner's tombstone-then-
    insert pattern is exercised in test_action_proposer.py.
    """
    if surfaced_at is None:
        surfaced_at = _now()
    if payload is None:
        payload = {
            "from_currency": "USD",
            "to_currency": "NIS",
            "amount_source_ccy": 40000,
        }
    row = ActionProposal(
        user_id=user_id,
        summary=f"Test {kind} proposal",
        rationale_md=f"# {kind}\n\nTest rationale.",
        suggested_payload=json.dumps(payload),
        severity=severity,
        surfaced_at=surfaced_at,
        expires_at=surfaced_at + timedelta(days=30),
        status=status,
        kind=kind,
        dedup_key=dedup_key,
        execution_state="proposed",
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# list_open_action_proposals
# ---------------------------------------------------------------------------


def test_list_returns_only_open_rows_severity_sorted(sync_session):
    """Spec §6.1: only status='open' rows; sort critical > warning >
    info, then surfaced_at desc within each severity band."""
    # Mix of statuses + severities + surface times.
    now = _now()
    a = _seed_proposal(
        sync_session, kind="allocate", severity="info",
        surfaced_at=now - timedelta(hours=2), status="open",
    )
    b = _seed_proposal(
        sync_session, kind="repatriate_currency", severity="critical",
        surfaced_at=now - timedelta(hours=5), status="open",
    )
    c = _seed_proposal(
        sync_session, kind="rebalance", severity="warning",
        surfaced_at=now - timedelta(hours=1), status="open",
    )
    _decided = _seed_proposal(
        sync_session, kind="note_only", severity="critical",
        surfaced_at=now, status="accepted",
    )
    _rejected = _seed_proposal(
        sync_session, kind="note_only", severity="critical",
        surfaced_at=now, status="rejected",
    )

    rows = list_open_action_proposals(sync_session, USER)

    # accepted + rejected rows must NOT appear.
    ids = [r.id for r in rows]
    assert _decided.id not in ids
    assert _rejected.id not in ids
    assert len(rows) == 3

    # Sort: critical > warning > info; then surfaced_at desc.
    assert rows[0].id == b.id  # critical
    assert rows[1].id == c.id  # warning
    assert rows[2].id == a.id  # info


# ---------------------------------------------------------------------------
# accept_action_proposal
# ---------------------------------------------------------------------------


def test_accept_plain_flips_status_and_execution_state(sync_session):
    """Plain Accept (no custom_payload):

      * status: open -> accepted
      * execution_state: proposed -> accepted_pending_user_action
      * decided_at: stamped
      * suggested_payload: untouched
    """
    row = _seed_proposal(sync_session)
    original_payload = row.suggested_payload
    now = _now() + timedelta(hours=1)

    updated = accept_action_proposal(
        sync_session, row.id, user_id=USER, now=now,
    )

    assert updated.id == row.id
    assert updated.status == "accepted"
    assert updated.execution_state == "accepted_pending_user_action"
    assert updated.decided_at == now
    assert updated.suggested_payload == original_payload
    # Customize note must NOT have been written on the plain path.
    assert updated.decided_by_user_note is None


def test_accept_customize_persists_edited_payload(sync_session):
    """Customize Accept overwrites suggested_payload with the user's
    edit + records the change in decided_by_user_note."""
    row = _seed_proposal(sync_session, kind="allocate", payload={
        "ticker": "SCHG", "amount_usd": 5000,
    })
    custom = {"ticker": "VTI", "amount_usd": 6000}

    updated = accept_action_proposal(
        sync_session, row.id, user_id=USER, custom_payload=custom,
    )

    assert updated.status == "accepted"
    assert updated.execution_state == "accepted_pending_user_action"
    assert json.loads(updated.suggested_payload) == custom
    assert updated.decided_by_user_note is not None
    assert "customized:" in updated.decided_by_user_note


def test_accept_rejects_non_open_row(sync_session):
    """A stale Accept POST on an already-decided row must fail loudly
    (translated to 409 at the route layer)."""
    row = _seed_proposal(sync_session, status="rejected")
    with pytest.raises(InvalidProposalStateError):
        accept_action_proposal(sync_session, row.id, user_id=USER)


def test_accept_cross_tenant_returns_not_found(sync_session):
    """A user_id mismatch returns ProposalNotFoundError (404) so
    existence isn't leaked across tenants."""
    other = User(id="other_user", plan="free")
    sync_session.add(other)
    sync_session.commit()
    row = _seed_proposal(sync_session, user_id="other_user")
    with pytest.raises(ProposalNotFoundError):
        accept_action_proposal(sync_session, row.id, user_id=USER)


# ---------------------------------------------------------------------------
# defer_action_proposal
# ---------------------------------------------------------------------------


def test_defer_flips_status_and_encodes_date(sync_session):
    """Defer:

      * status: open -> deferred
      * decided_at: stamped
      * decided_by_user_note: 'defer_until=<iso>; <note>'
      * execution_state: STAYS at 'proposed' (defer is not consent —
        codex BLOCKER #1 / no-execution invariant)
    """
    from datetime import date as _date

    row = _seed_proposal(sync_session)
    defer_until = _date(2026, 6, 15)
    now = _now() + timedelta(hours=1)

    updated = defer_action_proposal(
        sync_session, row.id, defer_until,
        user_id=USER, note="will revisit after paycheck", now=now,
    )

    assert updated.status == "deferred"
    assert updated.decided_at == now
    # execution_state preserved — the load-bearing no-execution pin.
    assert updated.execution_state == "proposed"
    assert updated.decided_by_user_note is not None
    assert "defer_until=2026-06-15" in updated.decided_by_user_note
    assert "will revisit after paycheck" in updated.decided_by_user_note


# ---------------------------------------------------------------------------
# reject_action_proposal
# ---------------------------------------------------------------------------


def test_reject_flips_status_and_dismisses_execution_state(sync_session):
    """Reject:

      * status: open -> rejected
      * execution_state: proposed -> dismissed (terminal)
      * decided_at: stamped
      * decided_by_user_note: reason (or NULL)
    """
    row = _seed_proposal(sync_session, kind="rebalance")
    now = _now() + timedelta(hours=1)

    updated = reject_action_proposal(
        sync_session, row.id, user_id=USER,
        reason="not aligned with current target allocation",
        now=now,
    )

    assert updated.status == "rejected"
    assert updated.execution_state == "dismissed"
    assert updated.decided_at == now
    assert updated.decided_by_user_note == (
        "not aligned with current target allocation"
    )


# ---------------------------------------------------------------------------
# No-execution invariant pin (codex BLOCKER #1 / spec §2.2.1)
# ---------------------------------------------------------------------------


def test_no_execution_state_ever_advances_past_pending_user_action(
    sync_session,
):
    """The service module must NEVER set ``execution_state`` to any
    value outside the three-state enum, AND must never advance a row
    past ``accepted_pending_user_action``. This pin walks every
    lifecycle path through the service.

    The matching CHECK constraint at the DB layer (migration 0055)
    is the structural defense; this test is the runtime mirror.
    """
    allowed = {"proposed", "accepted_pending_user_action", "dismissed"}

    # Path 1: Accept -> accepted_pending_user_action.
    r1 = _seed_proposal(sync_session, kind="allocate")
    out1 = accept_action_proposal(sync_session, r1.id, user_id=USER)
    assert out1.execution_state in allowed
    assert out1.execution_state == "accepted_pending_user_action"

    # Path 2: Customize-Accept -> accepted_pending_user_action.
    r2 = _seed_proposal(sync_session, kind="allocate")
    out2 = accept_action_proposal(
        sync_session, r2.id, user_id=USER,
        custom_payload={"ticker": "VTI", "amount_usd": 100},
    )
    assert out2.execution_state in allowed
    assert out2.execution_state == "accepted_pending_user_action"

    # Path 3: Defer -> proposed (untouched).
    r3 = _seed_proposal(sync_session, kind="allocate")
    out3 = defer_action_proposal(
        sync_session, r3.id, user_id=USER,
    )
    assert out3.execution_state in allowed
    assert out3.execution_state == "proposed"

    # Path 4: Reject -> dismissed.
    r4 = _seed_proposal(sync_session, kind="allocate")
    out4 = reject_action_proposal(
        sync_session, r4.id, user_id=USER, reason="no",
    )
    assert out4.execution_state in allowed
    assert out4.execution_state == "dismissed"

    # Structural defense: the service module imports NO broker /
    # execution / fx-execution / order-placement modules. Catches a
    # future regression where someone wires up auto-execution to the
    # Accept handler. AST-based so docstring prose mentioning these
    # names (e.g. in the no-execution invariant note above) doesn't
    # false-positive.
    import ast
    import argosy.services.action_proposals as svc

    src_path = svc.__file__
    assert src_path is not None
    with open(src_path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source)

    forbidden_module_prefixes = (
        "argosy.services.brokers",
        "argosy.adapters.schwab",
        "argosy.adapters.leumi",
        "argosy.services.fx_execution",
        "argosy.services.execution",
    )
    forbidden_call_names = {
        "place_order", "submit_order", "execute_order",
        "transfer_funds", "send_order",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for pref in forbidden_module_prefixes:
                    assert not alias.name.startswith(pref), (
                        f"forbidden import {alias.name!r} in "
                        "action_proposals service (codex BLOCKER #1)"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for pref in forbidden_module_prefixes:
                assert not mod.startswith(pref), (
                    f"forbidden 'from {mod} import ...' in "
                    "action_proposals service (codex BLOCKER #1)"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and name in forbidden_call_names:
                raise AssertionError(
                    f"forbidden call {name!r} in action_proposals "
                    "service (codex BLOCKER #1)"
                )


# ---------------------------------------------------------------------------
# to_view smoke (used by the route layer)
# ---------------------------------------------------------------------------


def test_to_view_round_trips_payload(sync_session):
    row = _seed_proposal(sync_session, payload={
        "from_currency": "USD",
        "to_currency": "NIS",
        "amount_source_ccy": 12345.67,
    })
    view = to_view(row)
    assert view.id == row.id
    assert view.suggested_payload == {
        "from_currency": "USD",
        "to_currency": "NIS",
        "amount_source_ccy": 12345.67,
    }
    assert view.execution_state == "proposed"
    assert view.status == "open"
