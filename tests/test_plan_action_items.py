"""Tests for the home-page action items widget endpoint.

Covers:
  * Status classification (TODAY / OVERDUE / DUE_SOON / UPCOMING)
  * Pending-draft preferred over current accepted plan
  * Fallback to current accepted plan when no draft
  * Empty case (no draft + no current) returns 200 + empty list
  * Sort order ASC by date (overdue first because their date is past)
  * Window-days cutoff drops actions too far in the future
  * Parameterized actions surface when their text embeds a date
  * Actions without parseable dates are skipped
  * cited_sources + plan_version_id wired through
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from argosy.state.models import PlanVersion, User


def _seed_user(client_with_db, user_id: str = "ariel") -> None:
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, user_id) is None:
            sess.add(User(id=user_id, plan="free"))
            sess.commit()
    finally:
        sess.close()


def _make_short_actions_json(actions: list[dict]) -> str:
    """Wrap a list of action dicts in the horizon-section envelope."""
    return json.dumps(
        {
            "horizon": "short",
            "freshness_expected": "monthly",
            "status": "major_revision",
            "posture": "test",
            "targets": [],
            "themes": [],
            "actions": actions,
            "deltas_from_prior": [],
            "rationale": "",
            "cited_sources": [],
        }
    )


def _make_medium_actions_json(actions: list[dict]) -> str:
    return json.dumps(
        {
            "horizon": "medium",
            "freshness_expected": "quarterly",
            "status": "minor_revision",
            "posture": "test",
            "targets": [],
            "themes": [],
            "actions": actions,
            "deltas_from_prior": [],
            "rationale": "",
            "cited_sources": [],
        }
    )


def _seed_draft(
    client_with_db,
    *,
    short_actions: list[dict] | None = None,
    medium_actions: list[dict] | None = None,
    user_id: str = "ariel",
) -> int:
    """Insert a role='draft' PlanVersion with the supplied actions."""
    _seed_user(client_with_db, user_id)
    sess = client_with_db.app.state.session_factory()
    try:
        pv = PlanVersion(
            user_id=user_id,
            role="draft",
            version_label="test-draft",
            raw_markdown="",
            horizon_short_json=_make_short_actions_json(short_actions or []),
            horizon_medium_json=_make_medium_actions_json(medium_actions or []),
        )
        sess.add(pv)
        sess.commit()
        sess.refresh(pv)
        return pv.id
    finally:
        sess.close()


def _seed_current(
    client_with_db,
    *,
    short_actions: list[dict] | None = None,
    medium_actions: list[dict] | None = None,
    user_id: str = "ariel",
) -> int:
    _seed_user(client_with_db, user_id)
    sess = client_with_db.app.state.session_factory()
    try:
        pv = PlanVersion(
            user_id=user_id,
            role="current",
            version_label="test-current",
            raw_markdown="",
            horizon_short_json=_make_short_actions_json(short_actions or []),
            horizon_medium_json=_make_medium_actions_json(medium_actions or []),
            accepted_at=datetime.now(timezone.utc),
        )
        sess.add(pv)
        sess.commit()
        sess.refresh(pv)
        return pv.id
    finally:
        sess.close()


def _today_iso(offset_days: int = 0) -> str:
    return (date.today() + timedelta(days=offset_days)).isoformat()


# ---------- Empty / no-data cases ----------------------------------------


def test_no_draft_no_current_returns_empty_list(client_with_db):
    """When the user has neither a pending draft nor a current accepted
    plan, the endpoint returns 200 with an empty list and zero counts."""
    _seed_user(client_with_db, "newcomer")
    r = client_with_db.get("/api/plan/action-items?user_id=newcomer")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["next_due"] is None
    assert body["overdue_count"] == 0
    assert body["today_count"] == 0
    assert body["upcoming_count"] == 0


def test_draft_with_no_actions_returns_empty(client_with_db):
    """A draft exists but has no dated actions — still 200 + empty."""
    _seed_draft(client_with_db, short_actions=[], medium_actions=[])
    r = client_with_db.get("/api/plan/action-items?user_id=ariel")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


# ---------- Status classification ----------------------------------------


def test_today_classification(client_with_db):
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "NVDA tranche execute",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(0),
                "detail": "200 sh trim",
                "rationale": "tax-gated",
                "cited_sources": ["agent_report:TaxAnalystAgent"],
            }
        ],
    )
    r = client_with_db.get("/api/plan/action-items?user_id=ariel")
    body = r.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["status"] == "TODAY"
    assert item["days_until"] == 0
    assert item["dated"] == _today_iso(0)
    assert body["today_count"] == 1
    assert body["overdue_count"] == 0


def test_overdue_classification(client_with_db):
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Attorney engagement",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(-3),
                "detail": "3 days ago",
                "rationale": "estate gate",
                "cited_sources": [],
            }
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    assert body["items"][0]["status"] == "OVERDUE"
    assert body["items"][0]["days_until"] == -3
    assert body["overdue_count"] == 1


def test_due_soon_classification(client_with_db):
    """Dated within +1..+3 days inclusive is DUE_SOON."""
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Tomorrow",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(1),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
            {
                "label": "Three days out",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(3),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    statuses = [it["status"] for it in body["items"]]
    assert statuses == ["DUE_SOON", "DUE_SOON"]
    assert body["upcoming_count"] == 2


def test_upcoming_classification(client_with_db):
    """+4 .. +window_days is UPCOMING."""
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Four days out",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(4),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            }
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    assert body["items"][0]["status"] == "UPCOMING"
    assert body["upcoming_count"] == 1


# ---------- Sort order ---------------------------------------------------


def test_sort_order_overdue_first_then_ascending(client_with_db):
    """Items are sorted ASC by date so the overdue (negative days_until)
    comes first, then TODAY, then upcoming."""
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Upcoming",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(5),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
            {
                "label": "Today",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(0),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
            {
                "label": "Overdue",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(-2),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    labels = [it["label"] for it in body["items"]]
    assert labels == ["Overdue", "Today", "Upcoming"]


# ---------- Fallback to current plan ------------------------------------


def test_falls_back_to_current_when_no_draft(client_with_db):
    """No pending draft → endpoint reads from role='current' instead."""
    _seed_current(
        client_with_db,
        short_actions=[
            {
                "label": "From current plan",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(2),
                "detail": "",
                "rationale": "from current",
                "cited_sources": [],
            }
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    assert len(body["items"]) == 1
    assert body["items"][0]["label"] == "From current plan"


def test_draft_preferred_over_current(client_with_db):
    """Both exist → endpoint reads from draft, not current."""
    _seed_current(
        client_with_db,
        short_actions=[
            {
                "label": "From current",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(2),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            }
        ],
    )
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "From draft",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(2),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            }
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    assert len(body["items"]) == 1
    assert body["items"][0]["label"] == "From draft"


# ---------- Window cutoff -----------------------------------------------


def test_window_days_filters_far_future_actions(client_with_db):
    """Actions whose date is past today + window_days are dropped."""
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Inside window",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(10),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
            {
                "label": "Outside window",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(30),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
        ],
    )
    # Default window is 14
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    labels = [it["label"] for it in body["items"]]
    assert labels == ["Inside window"]

    # Widen the window
    body = client_with_db.get(
        "/api/plan/action-items?user_id=ariel&window_days=60"
    ).json()
    labels = [it["label"] for it in body["items"]]
    assert labels == ["Inside window", "Outside window"]


def test_overdue_always_surfaced_regardless_of_window(client_with_db):
    """Past-due actions are kept even when far behind — window only
    caps the future side."""
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Very overdue",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(-90),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            }
        ],
    )
    body = client_with_db.get(
        "/api/plan/action-items?user_id=ariel&window_days=7"
    ).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["status"] == "OVERDUE"
    assert body["items"][0]["days_until"] == -90


# ---------- Parameterized actions with embedded dates ------------------


def test_parameterized_action_with_embedded_date_surfaces(client_with_db):
    """A parameterized action whose trigger_or_date text embeds a
    YYYY-MM-DD literal surfaces under the earliest mentioned date."""
    soon = _today_iso(5)
    later = _today_iso(10)
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Trip-wire rule",
                "horizon_kind": "parameterized",
                "trigger_or_date": (
                    f"IF quote NOT in hand by {later} OR attorney NOT "
                    f"engaged by {soon}: SUSPEND all realizations"
                ),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            }
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    assert len(body["items"]) == 1
    # Earliest of the two embedded dates wins.
    assert body["items"][0]["dated"] == soon


def test_directional_action_without_date_skipped(client_with_db):
    """Directional actions with trigger_or_date=None are dropped."""
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Directional, no date",
                "horizon_kind": "directional",
                "trigger_or_date": None,
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
            {
                "label": "Dated keeper",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(2),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    assert [it["label"] for it in body["items"]] == ["Dated keeper"]


# ---------- Field-level wiring ------------------------------------------


def test_response_carries_cited_sources_and_plan_version_id(client_with_db):
    pv_id = _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Wired action",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(1),
                "detail": "detail body",
                "rationale": "x" * 250,  # > 200 chars: must be truncated
                "cited_sources": [
                    "agent_report:TaxAnalystAgent",
                    "user_context.dependents_ages",
                ],
            }
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    item = body["items"][0]
    assert item["plan_version_id"] == pv_id
    assert item["cited_sources"] == [
        "agent_report:TaxAnalystAgent",
        "user_context.dependents_ages",
    ]
    assert len(item["rationale"]) == 200
    assert item["horizon"] == "short"
    assert item["detail"] == "detail body"
    # item_id is slug-derived
    assert item["item_id"].startswith("short.actions.")


def test_medium_horizon_actions_also_surfaced(client_with_db):
    """Medium-horizon actions count too — not only short."""
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Short one",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(2),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            }
        ],
        medium_actions=[
            {
                "label": "Medium one",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(5),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            }
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    horizons = sorted({it["horizon"] for it in body["items"]})
    assert horizons == ["medium", "short"]


def test_next_due_is_earliest_future_date(client_with_db):
    """next_due reflects the soonest future date, ignoring overdues."""
    _seed_draft(
        client_with_db,
        short_actions=[
            {
                "label": "Overdue",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(-5),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
            {
                "label": "Future #1",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(2),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
            {
                "label": "Future #2",
                "horizon_kind": "dated",
                "trigger_or_date": _today_iso(8),
                "detail": "",
                "rationale": "",
                "cited_sources": [],
            },
        ],
    )
    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    assert body["next_due"] == _today_iso(2)
    assert body["overdue_count"] == 1
    assert body["upcoming_count"] == 2  # Future #1 + Future #2 (both DUE_SOON/UPCOMING)
