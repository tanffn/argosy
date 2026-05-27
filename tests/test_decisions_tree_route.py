"""Tests for GET /api/decisions/{id}/agent-tree (T0.5).

The route is a thin HTTP wrapper around
``argosy.services.agent_tree_builder.build_agent_tree``. The deep topology
+ adapter wiring is already covered by ``tests/test_agent_tree_builder.py``;
here we only assert:

  1. Happy path — FM appears at the root, decision_kind matches, and the
     response shape is the dataclass-asdict JSON the UI consumes.
  2. 404 for an unknown decision_run_id.
  3. 404 for a run belonging to a different user (no existence leak).
  4. 404 for a run whose decision_kind isn't a synthesis kind.
  5. /api/decisions/{id}/replay carries ``agent_tree_url`` so the UI
     knows where to fetch the tree (no separate discovery hop).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from argosy.state.models import (
    AgentReport,
    DecisionPhase,
    DecisionRun,
    User,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _seed_users(client_with_db):
    """Make sure both 'ariel' and 'bob' exist so FK-aware sessions are happy."""
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        if sess.get(User, "bob") is None:
            sess.add(User(id="bob", plan="free"))
        sess.commit()
    finally:
        sess.close()


def _seed_synthesis_run(
    client_with_db,
    *,
    user_id: str = "ariel",
    decision_kind: str = "plan_revision",
    run_id_hint: int | None = None,
) -> int:
    """Insert a minimal synthesis run + a fund_manager + plan_synthesizer
    + risk_facilitator agent_report so the builder has something non-trivial
    to return. Mirrors the topology that ``test_agent_tree_builder._seed_synthesis_run``
    uses, just trimmed to the rows the route-level assertions need.

    Returns the new ``decision_runs.id``.
    """
    sess = client_with_db.app.state.session_factory()
    try:
        now = datetime.now(timezone.utc)
        run = DecisionRun(
            user_id=user_id,
            ticker="(plan)",
            tier=None,
            decision_kind=decision_kind,
            status="completed",
            started_at=now,
            finished_at=now,
        )
        if run_id_hint is not None:
            run.id = run_id_hint
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        rid = run.id

        decision_id_str = f"plan-synth-{rid}"

        # Seed an FM + plan_synthesizer + risk_facilitator + one analyst so
        # the tree's root has confident agent_role + identifiable subtree
        # without forcing us to seed all 18 rows.
        for role, conf in (
            ("fund_manager", None),
            ("plan_synthesizer", "MEDIUM"),
            ("risk_facilitator", "MEDIUM"),
            ("news", "HIGH"),
        ):
            sess.add(AgentReport(
                user_id=user_id,
                agent_role=role,
                decision_id=decision_id_str,
                response_text=f"{role} ok",
                confidence=conf,
                model="claude-opus-4-7" if role == "fund_manager" else "claude-sonnet-4-6",
                tokens_in=10,
                tokens_out=20,
                cost_usd=0.001,
            ))

        sess.add(DecisionPhase(
            decision_run_id=rid,
            user_id=user_id,
            seq=1,
            kind="synthesis.phase_1",
            started_at=now,
            finished_at=now,
            participants_json="[]",
            phase_output_json=json.dumps({
                "phase": 1,
                "adapter_outcomes": [
                    {
                        "adapter_name": "finnhub_news",
                        "target": "NVDA",
                        "status": "ok",
                        "latency_ms": 120,
                        "payload_size_bytes": 9001,
                        "http_status_code": 200,
                        "error_text": None,
                    },
                ],
            }),
        ))
        sess.commit()
        return rid
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_agent_tree_returns_fm_at_root_for_synthesis_run(
    client_with_db, _seed_users,
) -> None:
    """FM appears at root for run #23-shaped seed (plan_revision kind).

    The plan asks for "FM at root for run #23 (user_id: ariel)" — the
    underlying ``argosy.db`` row isn't available in the tmp_path test DB,
    so we replicate the same kind/role topology against the per-test
    SQLite. The route logic doesn't care whether the row id is 23 or
    something else; we still pass ``run_id_hint=23`` to keep the
    assertion text honest.
    """
    rid = _seed_synthesis_run(
        client_with_db, user_id="ariel", run_id_hint=23,
    )
    r = client_with_db.get(
        f"/api/decisions/{rid}/agent-tree?user_id=ariel"
    )
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["decision_run_id"] == rid
    assert body["decision_kind"] == "plan_revision"
    assert body["root"]["agent_role"] == "fund_manager"

    # FM's children include synth + risk_facilitator + plan_critique
    # (the last two are "skipped" since we didn't seed them) + the
    # codex_second_opinion second-opinion node (skipped here — codex was
    # added in commit 0bedd9b and not seeded in this minimal fixture).
    child_roles = [c["agent_role"] for c in body["root"]["children"]]
    assert child_roles == [
        "plan_synthesizer",
        "risk_facilitator",
        "plan_critique",
        "codex_second_opinion",
    ]

    # Status summary should be present and have integer counts.
    summary = body["status_summary"]
    assert summary["agents_ok"] >= 1
    assert isinstance(summary["agents_failed"], int)
    assert summary["adapters_ok"] == 1
    assert summary["adapters_failed"] == 0

    # Dataclass-asdict serialization sanity: nested children are plain
    # dicts, adapter list is a list, cost_usd is float-typed.
    fm = body["root"]
    assert isinstance(fm["children"], list)
    assert isinstance(fm["adapters"], list)
    assert fm["cost_usd"] is None or isinstance(fm["cost_usd"], float)


# ---------------------------------------------------------------------------
# 404 cases
# ---------------------------------------------------------------------------


def test_agent_tree_404_for_unknown_id(client_with_db, _seed_users) -> None:
    r = client_with_db.get("/api/decisions/9999/agent-tree?user_id=ariel")
    assert r.status_code == 404


def test_agent_tree_404_for_other_user(client_with_db, _seed_users) -> None:
    """Wrong user gets 404 — don't leak existence."""
    rid = _seed_synthesis_run(client_with_db, user_id="ariel")
    r = client_with_db.get(
        f"/api/decisions/{rid}/agent-tree?user_id=bob"
    )
    assert r.status_code == 404


def test_agent_tree_200_with_root_none_for_non_synthesis_kind(
    client_with_db, _seed_users,
) -> None:
    """T4.4: non-synthesis kinds (trade_proposal, delta_pushback, daily_brief,
    plan_amendment_chat) used to return 404; now they return 200 with
    root=None + unsupported_reason so the UI can render a kind-appropriate
    placeholder instead of an error.
    """
    rid = _seed_synthesis_run(
        client_with_db, user_id="ariel", decision_kind="trade_proposal",
    )
    r = client_with_db.get(
        f"/api/decisions/{rid}/agent-tree?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"] is None
    assert body["decision_kind"] == "trade_proposal"
    assert body["unsupported_reason"] is not None


def test_agent_tree_200_for_delta_pushback_kind(
    client_with_db, _seed_users,
) -> None:
    """T4.4: delta_pushback decision_kind is handled — non-synthesis kind
    surfaces as 200 with root=None, status_summary populated from the
    run's agent_reports (zero in this minimal seed)."""
    rid = _seed_synthesis_run(
        client_with_db, user_id="ariel", decision_kind="delta_pushback",
    )
    r = client_with_db.get(
        f"/api/decisions/{rid}/agent-tree?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"] is None
    assert body["decision_kind"] == "delta_pushback"
    assert isinstance(body["status_summary"]["agents_ok"], int)


def test_agent_tree_200_for_daily_brief_kind(
    client_with_db, _seed_users,
) -> None:
    """T4.4: daily_brief decision_kind is handled — non-synthesis kind."""
    rid = _seed_synthesis_run(
        client_with_db, user_id="ariel", decision_kind="daily_brief",
    )
    r = client_with_db.get(
        f"/api/decisions/{rid}/agent-tree?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"] is None
    assert body["decision_kind"] == "daily_brief"


# ---------------------------------------------------------------------------
# Cross-route contract: replay must carry agent_tree_url.
# ---------------------------------------------------------------------------


def test_replay_response_includes_agent_tree_url(
    client_with_db, _seed_users,
) -> None:
    """The replay endpoint must surface the relative agent-tree URL so the
    UI can hop without rediscovering the route shape."""
    rid = _seed_synthesis_run(client_with_db, user_id="ariel")
    r = client_with_db.get(f"/api/decisions/{rid}/replay?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_tree_url"] == f"/api/decisions/{rid}/agent-tree"
