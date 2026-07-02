"""Decision-funnel calibration summary — the BETA exposure. Nothing hidden: the
funnel's graded decisions and how much data it has collected are surfaced (flagged
beta) rather than kept in shadow."""
from __future__ import annotations

from argosy.services.funnel_view import calibration_summary_payload


def test_off_state_is_exposed_not_hidden():
    """When the funnel isn't enabled yet, the surface still exists — it says so and
    shows zero collected, rather than hiding the capability."""
    p = calibration_summary_payload(
        decisions=0, runs=0, first_at=None, last_at=None, surfaced=0,
        would_surface=0, enabled=False, shadow=True, stage3=False,
    )
    assert p["beta"] is True
    assert p["status"] == "off"
    assert p["decisions_collected"] == 0
    assert "beta" in p["headline"].lower()


def test_collecting_state_shows_data_volume():
    """Enabled + shadow = calibrating: expose the count + span so 'more data needed'
    is quantified, not vague."""
    p = calibration_summary_payload(
        decisions=42, runs=15, first_at="2026-06-01T00:00:00+00:00",
        last_at="2026-07-01T00:00:00+00:00", surfaced=0, would_surface=7,
        enabled=True, shadow=True, stage3=True,
    )
    assert p["status"] == "collecting"
    assert p["decisions_collected"] == 42
    assert p["days_span"] == 30
    assert p["would_surface"] == 7
    assert "42" in p["headline"] and "30" in p["headline"]


def test_live_state_is_labelled():
    p = calibration_summary_payload(
        decisions=100, runs=40, first_at="2026-05-01T00:00:00+00:00",
        last_at="2026-07-01T00:00:00+00:00", surfaced=12, would_surface=12,
        enabled=True, shadow=False, stage3=True,
    )
    assert p["status"] == "live"
    assert p["surfaced"] == 12


def test_calibration_endpoint_exposes_beta_on_empty_db(client_with_db):
    """The endpoint always returns the beta surface (200) — the capability is shown
    even with no data yet, rather than hidden."""
    r = client_with_db.get("/api/decisions/funnel/calibration?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["beta"] is True
    assert body["decisions_collected"] == 0
    # Funnel is enabled by default (nothing-hidden) → calibrating with 0 data yet.
    assert body["status"] == "collecting"
