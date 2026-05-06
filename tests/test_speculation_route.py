"""API route tests for POST /api/plan/current/speculative/{ticker}/take.

Wave 3 / Task 3.4. Verifies the FastAPI surface that wraps
``argosy.orchestrator.speculation_router.route_accepted_candidate`` —
the unit-level behavior of the router itself is covered in
``tests/test_speculation_router.py``.
"""

from __future__ import annotations

from argosy.state.models import PlanVersion, User


def test_post_take_speculative_routes_to_argonaut(client_with_db, monkeypatch):
    """Clicking 'Take a swing' on a speculative candidate creates a T0 Argonaut proposal."""
    from argosy.orchestrator import speculation_router as router

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        sess.add(PlanVersion(
            user_id="ariel", role="current", version_label="x", raw_markdown="",
            horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
            horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x"}',
            horizon_short_json=(
                '{"horizon":"short","freshness_expected":"monthly","status":"no_change",'
                '"posture":"x","speculative_candidates":['
                '{"ticker":"HOOD","thesis_summary":"momentum",'
                '"suggested_position_usd":800,"suggested_position_pct_of_net_worth":0.0008,'
                '"risk_ceiling_check":true,"horizon_days":30,"expected_drawdown_pct":0.2,'
                '"exit_trigger":"stop -20%, take +50%","sourced_from":["sentiment"]}'
                ']}'
            ),
        ))
        sess.commit()
    finally:
        sess.close()

    monkeypatch.setattr(
        router, "_create_proposal",
        lambda **kw: type("P", (), {"id": 4242})(),
    )

    r = client_with_db.post(
        "/api/plan/current/speculative/HOOD/take?user_id=ariel&execution_mode=paper"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal_id"] == 4242
    assert body["ticker"] == "HOOD"
    assert body["paper"] is True


def test_post_take_speculative_404_unknown_ticker(client_with_db):
    r = client_with_db.post(
        "/api/plan/current/speculative/NOPE/take?user_id=ariel&execution_mode=paper"
    )
    assert r.status_code in (404, 400)
