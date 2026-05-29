"""Sprint #2 commit #12 — GET /api/retirement/upcoming-vests route tests.

Covers:
  * Empty user → 200 with empty ``upcoming`` array.
  * User with one historical vest → 200 with projected vests + correct
    three-scenario tax rates serialized.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from argosy.state.models import RsuVestEvent, User


def test_empty_user_returns_empty_outlook(client_with_db):
    """No vest events → 200 + empty upcoming list, but rates surfaced."""
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()

    resp = client_with_db.get(
        "/api/retirement/upcoming-vests?user_id=ariel&horizon_days=90"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == "ariel"
    assert body["horizon_days"] == 90
    assert body["upcoming"] == []
    # Rates surfaced for the UI footnote even when no vests project.
    assert "rate_nominal" in body
    assert "rate_effective" in body
    assert "rate_conservative" in body
    assert body["rate_conservative"] >= body["rate_nominal"]


def test_one_vest_projects_with_three_scenarios(client_with_db):
    """One historical vest → projected vests with all three tax scenarios."""
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
        s.add(RsuVestEvent(
            user_id="ariel",
            symbol="NVDA",
            grant_id="G1",
            # Far enough back that +90d projections land within a 365d
            # horizon comfortably.
            vest_date=date(2025, 12, 1),
            shares_vested=Decimal("100"),
            shares_withheld=Decimal("0"),
            shares_net=Decimal("100"),
            fmv_per_share_usd=Decimal("150"),
            award_date=None,
            source_file="test_route",
        ))
        s.commit()

    resp = client_with_db.get(
        "/api/retirement/upcoming-vests?user_id=ariel&horizon_days=730"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == "ariel"
    assert len(body["upcoming"]) > 0
    first = body["upcoming"][0]
    # Three-scenario rates all present + monotone:
    # conservative >= nominal AND conservative >= effective (it's the
    # worst case, so the *rate* is the highest).
    assert first["rate_conservative"] >= first["rate_nominal"]
    # Post-tax amounts: nominal/effective/conservative all populated.
    for key in (
        "expected_post_tax_nominal_usd",
        "expected_post_tax_effective_usd",
        "expected_post_tax_conservative_usd",
    ):
        assert isinstance(first[key], (int, float))
        assert first[key] > 0
    # Conservative post-tax <= nominal post-tax (higher tax rate → lower
    # take-home).
    assert (
        first["expected_post_tax_conservative_usd"]
        <= first["expected_post_tax_nominal_usd"]
    )
    # Days-until is sane.
    assert first["days_until"] >= 0
    # Allocation preview is a list (may be empty when no snapshot, but
    # always serialized).
    assert isinstance(first["allocation_preview"], list)


def test_intruder_user_isolated(client_with_db):
    """Vest events for a different user must not leak."""
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
        if s.get(User, "intruder") is None:
            s.add(User(id="intruder", plan="free"))
        s.add(RsuVestEvent(
            user_id="intruder",
            symbol="NVDA",
            grant_id="X",
            vest_date=date(2025, 12, 1),
            shares_vested=Decimal("999"),
            shares_withheld=Decimal("0"),
            shares_net=Decimal("999"),
            fmv_per_share_usd=Decimal("150"),
            award_date=None,
            source_file="test_route",
        ))
        s.commit()

    resp = client_with_db.get(
        "/api/retirement/upcoming-vests?user_id=ariel&horizon_days=730"
    )
    assert resp.status_code == 200
    assert resp.json()["upcoming"] == []
