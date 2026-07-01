"""Task 7: the shadow preflight is attached to /deploy-cash only behind the
kill switch, and never breaks the route. Full preflight LOGIC is covered by
tests/services/deployment_funnel/; here we assert the wiring guard."""
from __future__ import annotations


def test_deploy_cash_preflight_absent_when_disabled(client_with_db, monkeypatch):
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_FUNNEL_ENABLED", "0")
    from argosy.config import get_settings

    get_settings.cache_clear()
    r = client_with_db.get(
        "/api/portfolio/deploy-cash?user_id=ariel&cash_usd=100000"
    )
    assert r.status_code == 200, r.text
    assert r.json().get("preflight") is None
    get_settings.cache_clear()


def test_deploy_cash_enabled_never_500s(client_with_db, monkeypatch):
    # With the flag on but (in the test DB) possibly no accepted plan, the route
    # must still return 200 — the preflight is additive + guarded, never fatal.
    monkeypatch.setenv("ARGOSY_DEPLOYMENT_FUNNEL_ENABLED", "1")
    from argosy.config import get_settings

    get_settings.cache_clear()
    r = client_with_db.get(
        "/api/portfolio/deploy-cash?user_id=ariel&cash_usd=100000"
    )
    assert r.status_code == 200, r.text
    # preflight is present only when an accepted plan doc exists; either way,
    # the field must be a valid shape (None or an object) and not crash.
    pf = r.json().get("preflight")
    assert pf is None or "enriched" in pf
    get_settings.cache_clear()
