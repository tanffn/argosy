"""Tests for the /api/fleet-self-review/{list,trends} routes.

Covers:
  * GET /list — newest-first, severity_summary + findings_total are
    pre-computed per row, tenant-scoped.
  * GET /trends — N points covering the recent window, most-persistent
    detectors computed across reports.
  * Empty case — no rows → trends returns ``{points: [], ...}``.

The existing single-report endpoints (``/latest`` / ``/{id}`` /
``/run``) are NOT exercised here — they're covered by
``test_fleet_self_review.py``.  This file is additive coverage for
the new list + trends surface.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import FleetSelfReviewReport, User


USER = "ariel"


def _make_finding(
    *, detector: str, severity: str, title: str, suffix: str = "x",
) -> dict:
    """Build a JSON-serialisable Finding dict for fixture seeding."""
    return {
        "id": f"{detector}:{suffix}",
        "detector": detector,
        "severity": severity,
        "category": "data_quality",
        "title": title,
        "evidence": {},
        "suggested_fix": "see runbook",
    }


def _seed_report(
    session: Session,
    *,
    user_id: str,
    generated_at: datetime,
    findings: list[dict],
    scope_kind: str = "post_synthesis",
    decision_run_id: int | None = None,
) -> FleetSelfReviewReport:
    """Insert one fleet_self_review_reports row with the given findings.

    Severity counts are derived from the findings list so tests don't
    have to keep two representations in sync.
    """
    sev = {"RED": 0, "AMBER": 0, "YELLOW": 0}
    for f in findings:
        s = f.get("severity")
        if s in sev:
            sev[s] += 1
    row = FleetSelfReviewReport(
        user_id=user_id,
        generated_at=generated_at,
        scope_kind=scope_kind,
        decision_run_id=decision_run_id,
        content_md="# fleet self-review (test)\n",
        findings_json=json.dumps(findings),
        severity_summary_json=json.dumps(sev),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.fixture
def seeded_user(client_with_db):
    """Ensure ``ariel`` (and an intruder) exist as users.  No reports
    yet — each test decides what to insert."""
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, USER) is None:
            s.add(User(id=USER, plan="free"))
        if s.get(User, "intruder") is None:
            s.add(User(id="intruder", plan="free"))
        s.commit()
    return client_with_db


# ----------------------------------------------------------------------
# /list
# ----------------------------------------------------------------------


def test_list_empty_returns_empty_array(seeded_user):
    """No reports for the user → endpoint returns 200 + []."""
    resp = seeded_user.get(f"/api/fleet-self-review/list?user_id={USER}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_list_newest_first_with_severity_and_total(seeded_user):
    """List returns newest-first; severity_summary + findings_total are
    per-row."""
    SessionLocal = seeded_user.app.state.session_factory
    base = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    with SessionLocal() as s:
        # Oldest first in the loop so the IDs and times are clearly ordered;
        # the endpoint must still return newest-first.
        _seed_report(
            s, user_id=USER, generated_at=base,
            findings=[
                _make_finding(detector="D6", severity="YELLOW",
                              title="Cost outlier", suffix="r1a"),
            ],
        )
        _seed_report(
            s, user_id=USER, generated_at=base + timedelta(hours=1),
            findings=[
                _make_finding(detector="D7", severity="RED",
                              title="Stuck run", suffix="r2a"),
                _make_finding(detector="D6", severity="YELLOW",
                              title="Cost outlier", suffix="r2b"),
                _make_finding(detector="D6", severity="YELLOW",
                              title="Cost outlier", suffix="r2c"),
            ],
        )
        _seed_report(
            s, user_id=USER, generated_at=base + timedelta(hours=2),
            findings=[
                _make_finding(detector="D4", severity="AMBER",
                              title="Unknown source", suffix="r3a"),
                _make_finding(detector="D4", severity="AMBER",
                              title="Unknown source", suffix="r3b"),
            ],
        )
        # Another user — must NOT leak across the tenant boundary.
        _seed_report(
            s, user_id="intruder", generated_at=base + timedelta(hours=3),
            findings=[
                _make_finding(detector="D1", severity="RED",
                              title="leak", suffix="leak"),
            ],
        )

    resp = seeded_user.get(f"/api/fleet-self-review/list?user_id={USER}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 3, body  # intruder's row excluded

    # Newest first.
    times = [r["generated_at"] for r in body]
    assert times == sorted(times, reverse=True), times

    # Newest row: 2 AMBER, 2 findings total.
    newest = body[0]
    assert newest["severity_summary"] == {"RED": 0, "AMBER": 2, "YELLOW": 0}
    assert newest["findings_total"] == 2

    # Middle row: 1 RED + 2 YELLOW, 3 findings total.
    middle = body[1]
    assert middle["severity_summary"] == {"RED": 1, "AMBER": 0, "YELLOW": 2}
    assert middle["findings_total"] == 3

    # Oldest row.
    oldest = body[2]
    assert oldest["severity_summary"] == {"RED": 0, "AMBER": 0, "YELLOW": 1}
    assert oldest["findings_total"] == 1


def test_list_respects_limit(seeded_user):
    """``limit`` query param caps the response length."""
    SessionLocal = seeded_user.app.state.session_factory
    base = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    with SessionLocal() as s:
        for i in range(5):
            _seed_report(
                s, user_id=USER, generated_at=base + timedelta(minutes=i),
                findings=[],
            )
    resp = seeded_user.get(
        f"/api/fleet-self-review/list?user_id={USER}&limit=2"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2


# ----------------------------------------------------------------------
# /trends
# ----------------------------------------------------------------------


def test_trends_empty_returns_empty_points(seeded_user):
    """No reports → 200 + empty points + empty most_persistent_findings."""
    resp = seeded_user.get(f"/api/fleet-self-review/trends?user_id={USER}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "points": [],
        "days": 30,
        "report_count": 0,
        "most_persistent_findings": [],
    }


def test_trends_returns_points_in_ascending_order(seeded_user):
    """Three reports inside the window → three points, oldest first."""
    SessionLocal = seeded_user.app.state.session_factory
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with SessionLocal() as s:
        for offset_h in (1, 5, 24):
            _seed_report(
                s, user_id=USER,
                generated_at=now - timedelta(hours=offset_h),
                findings=[
                    _make_finding(detector="D7", severity="RED",
                                  title="Stuck run", suffix=f"h{offset_h}"),
                    _make_finding(detector="D6", severity="YELLOW",
                                  title="Cost outlier", suffix=f"h{offset_h}b"),
                ],
            )
    resp = seeded_user.get(
        f"/api/fleet-self-review/trends?user_id={USER}&days=7"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["report_count"] == 3
    assert body["days"] == 7

    points = body["points"]
    assert len(points) == 3
    # ASC by generated_at.
    times = [p["generated_at"] for p in points]
    assert times == sorted(times), times

    # Each point has 1 RED + 1 YELLOW.
    for p in points:
        assert p["red"] == 1
        assert p["amber"] == 0
        assert p["yellow"] == 1


def test_trends_window_excludes_older_reports(seeded_user):
    """Reports outside the lookback window must NOT appear in trends."""
    SessionLocal = seeded_user.app.state.session_factory
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with SessionLocal() as s:
        # 2 inside the window
        _seed_report(
            s, user_id=USER, generated_at=now - timedelta(days=1),
            findings=[],
        )
        _seed_report(
            s, user_id=USER, generated_at=now - timedelta(days=4),
            findings=[],
        )
        # 1 outside the window (much older)
        _seed_report(
            s, user_id=USER, generated_at=now - timedelta(days=60),
            findings=[],
        )
    resp = seeded_user.get(
        f"/api/fleet-self-review/trends?user_id={USER}&days=7"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["report_count"] == 2
    assert len(body["points"]) == 2


def test_trends_most_persistent_findings(seeded_user):
    """A finding present in ≥50% of reports is flagged as persistent;
    one present in <50% is not.

    Setup: 4 reports.  D4:fx and D6:cost appear in 3/4 (75%).  D7:stuck
    appears in 1/4 (25%) — must not surface.
    """
    SessionLocal = seeded_user.app.state.session_factory
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with SessionLocal() as s:
        # Report 1 — D4, D6, D7
        _seed_report(
            s, user_id=USER, generated_at=now - timedelta(hours=4),
            findings=[
                _make_finding(detector="D4", severity="AMBER",
                              title="unknown sources in fx",
                              suffix="fx"),
                _make_finding(detector="D6", severity="YELLOW",
                              title="cost outlier",
                              suffix="cost"),
                _make_finding(detector="D7", severity="RED",
                              title="stuck run",
                              suffix="stuck"),
            ],
        )
        # Report 2 — D4, D6 only
        _seed_report(
            s, user_id=USER, generated_at=now - timedelta(hours=3),
            findings=[
                _make_finding(detector="D4", severity="AMBER",
                              title="unknown sources in fx",
                              suffix="fx"),
                _make_finding(detector="D6", severity="YELLOW",
                              title="cost outlier",
                              suffix="cost"),
            ],
        )
        # Report 3 — D4, D6 only
        _seed_report(
            s, user_id=USER, generated_at=now - timedelta(hours=2),
            findings=[
                _make_finding(detector="D4", severity="AMBER",
                              title="unknown sources in fx",
                              suffix="fx"),
                _make_finding(detector="D6", severity="YELLOW",
                              title="cost outlier",
                              suffix="cost"),
            ],
        )
        # Report 4 — empty (zero findings)
        _seed_report(
            s, user_id=USER, generated_at=now - timedelta(hours=1),
            findings=[],
        )

    resp = seeded_user.get(
        f"/api/fleet-self-review/trends?user_id={USER}&days=30"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["report_count"] == 4

    persistent = body["most_persistent_findings"]
    # D4:fx and D6:cost each present in 3/4 reports (75%) → persistent.
    persistent_joined = " | ".join(persistent)
    assert "unknown sources in fx" in persistent_joined, persistent
    assert "cost outlier" in persistent_joined, persistent
    # D7:stuck present in 1/4 (25%) → NOT persistent.
    assert "stuck run" not in persistent_joined, persistent
    # Each entry should report the run count.
    assert "(3 runs)" in persistent_joined, persistent


def test_trends_single_report_no_persistence_yet(seeded_user):
    """One sample isn't a pattern → most_persistent_findings is empty."""
    SessionLocal = seeded_user.app.state.session_factory
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with SessionLocal() as s:
        _seed_report(
            s, user_id=USER, generated_at=now - timedelta(hours=1),
            findings=[
                _make_finding(detector="D7", severity="RED",
                              title="Stuck", suffix="x"),
            ],
        )
    resp = seeded_user.get(
        f"/api/fleet-self-review/trends?user_id={USER}&days=7"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["report_count"] == 1
    assert body["most_persistent_findings"] == []


def test_trends_does_not_leak_across_tenants(seeded_user):
    """A report owned by another user must not appear in this user's
    trends — same boundary as /list."""
    SessionLocal = seeded_user.app.state.session_factory
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with SessionLocal() as s:
        _seed_report(
            s, user_id="intruder", generated_at=now - timedelta(hours=1),
            findings=[
                _make_finding(detector="D7", severity="RED",
                              title="leak", suffix="leak"),
            ],
        )
    resp = seeded_user.get(
        f"/api/fleet-self-review/trends?user_id={USER}&days=7"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["report_count"] == 0
    assert body["points"] == []
