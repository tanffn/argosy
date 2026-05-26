"""API route tests for POST /api/plan/current/speculative/{ticker}/take.

Wave 3 / Task 3.4. Verifies the FastAPI surface that wraps
``argosy.orchestrator.speculation_router.route_accepted_candidate`` —
the unit-level behavior of the router itself is covered in
``tests/test_speculation_router.py``.

T4.2 extension: also verifies that /api/proposals surfaces the new
``conviction``, ``cited_sources``, and ``tier`` fields on every row
(empty/None for non-speculation proposals; populated for
speculation-origin rows persisted via ``create_speculative_proposal``).
"""

from __future__ import annotations

import json

from argosy.state.models import PlanVersion, Proposal as ProposalRow, User


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
        # The router runs a C2 sanity check on ``account_class`` after the
        # helper returns, so the stub must expose that attribute (matching
        # the routed account-class string, "limited").
        lambda **kw: type(
            "P", (), {"id": 4242, "account_class": kw["account_class"]},
        )(),
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


# ----------------------------------------------------------------------
# T4.2: /api/proposals exposes conviction + cited_sources + tier
# ----------------------------------------------------------------------


def _seed_user(client_with_db, uid: str = "ariel") -> None:
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, uid) is None:
            sess.add(User(id=uid, plan="free"))
            sess.commit()
    finally:
        sess.close()


def test_proposals_list_surfaces_conviction_and_cited_sources(client_with_db):
    """T4.2: speculation-origin proposals expose conviction + citations.

    Seeds two proposals:
      - one speculation-origin row (account_class=limited, with
        sourced_from persisted into expected_impact_json),
      - one regular-pipeline row (account_class=main, no sourced_from).

    Asserts the route surfaces ``conviction``, ``cited_sources``, and
    ``tier`` on both, with cited_sources populated only on the
    speculation row.
    """
    _seed_user(client_with_db)
    SessionLocal = client_with_db.app.state.session_factory
    sess = SessionLocal()
    try:
        # Speculation-origin row: cited_sources lives in expected_impact_json.
        spec = ProposalRow(
            user_id="ariel",
            ticker="HOOD",
            action="buy",
            size_shares_or_currency=800,
            size_units="currency",
            instrument="stock",
            order_type="limit",
            tier="T0",
            account_class="limited",
            status="draft",
            rationale_summary="momentum",
            expected_impact_json=json.dumps({
                "exit_trigger": "stop -20%, take +50%",
                "execution_mode": "paper",
                "sourced_from": ["sentiment", "macro/yields"],
            }),
            confidence="HIGH",
        )
        # Regular row: no sourced_from, account_class=main.
        regular = ProposalRow(
            user_id="ariel",
            ticker="NVDA",
            action="sell",
            size_shares_or_currency=10,
            size_units="shares",
            instrument="stock",
            order_type="market",
            tier="T2",
            account_class="main",
            status="awaiting_human",
            rationale_summary="reduce concentration",
            expected_impact_json="{}",
            confidence="MEDIUM",
        )
        sess.add_all([spec, regular])
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/proposals?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    rows_by_ticker = {row["ticker"]: row for row in body["rows"]}

    hood = rows_by_ticker["HOOD"]
    # conviction mirrors confidence ("HIGH" was stored).
    assert hood["conviction"] == "HIGH"
    assert hood["cited_sources"] == ["sentiment", "macro/yields"]
    # tier is the existing column — just re-checking it is still present.
    assert hood["tier"] == "T0"
    # account_class identifies this as the Argonaut / limited bucket the
    # UI uses to split speculative from regular proposals.
    assert hood["account_class"] == "limited"

    nvda = rows_by_ticker["NVDA"]
    assert nvda["conviction"] == "MEDIUM"
    # No sourced_from in expected_impact_json → empty list (not None).
    assert nvda["cited_sources"] == []
    assert nvda["tier"] == "T2"
    assert nvda["account_class"] == "main"


def test_proposals_detail_surfaces_conviction_and_cited_sources(client_with_db):
    """T4.2: the detail route's embedded proposal also carries the new fields."""
    _seed_user(client_with_db)
    SessionLocal = client_with_db.app.state.session_factory
    sess = SessionLocal()
    try:
        spec = ProposalRow(
            user_id="ariel",
            ticker="GME",
            action="buy",
            size_shares_or_currency=500,
            size_units="currency",
            instrument="stock",
            order_type="limit",
            tier="T0",
            account_class="limited",
            status="draft",
            rationale_summary="catalyst",
            expected_impact_json=json.dumps({
                "exit_trigger": "stop -25%",
                "execution_mode": "paper",
                "sourced_from": ["sec_13f", "blogger_sentiment"],
            }),
            confidence="LOW",
        )
        sess.add(spec)
        sess.commit()
        pid = spec.id
    finally:
        sess.close()

    r = client_with_db.get(f"/api/proposals/{pid}?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal"]["conviction"] == "LOW"
    assert body["proposal"]["cited_sources"] == ["sec_13f", "blogger_sentiment"]
    assert body["proposal"]["tier"] == "T0"


def test_proposals_cited_sources_robust_to_bad_json(client_with_db):
    """T4.2: corrupt expected_impact_json must not blow up the route."""
    _seed_user(client_with_db)
    SessionLocal = client_with_db.app.state.session_factory
    sess = SessionLocal()
    try:
        bad = ProposalRow(
            user_id="ariel",
            ticker="BAD",
            action="buy",
            size_shares_or_currency=100,
            size_units="currency",
            instrument="stock",
            order_type="limit",
            tier="T0",
            account_class="limited",
            status="draft",
            rationale_summary="x",
            # Not valid JSON — the route must degrade to [] without raising.
            expected_impact_json="this is not json {{{",
            confidence="HIGH",
        )
        # And one with sourced_from of the wrong shape (a string instead
        # of a list) — must also degrade cleanly.
        wrong_shape = ProposalRow(
            user_id="ariel",
            ticker="ODD",
            action="buy",
            size_shares_or_currency=100,
            size_units="currency",
            instrument="stock",
            order_type="limit",
            tier="T0",
            account_class="limited",
            status="draft",
            rationale_summary="x",
            expected_impact_json=json.dumps({"sourced_from": "just-a-string"}),
            confidence=None,
        )
        sess.add_all([bad, wrong_shape])
        sess.commit()
    finally:
        sess.close()

    r = client_with_db.get("/api/proposals?user_id=ariel")
    assert r.status_code == 200, r.text
    rows_by_ticker = {row["ticker"]: row for row in r.json()["rows"]}
    assert rows_by_ticker["BAD"]["cited_sources"] == []
    assert rows_by_ticker["ODD"]["cited_sources"] == []
    # confidence=None must surface as conviction=None (not "None" string).
    assert rows_by_ticker["ODD"]["conviction"] is None


def test_speculation_router_persists_sourced_from(client_with_db, monkeypatch):
    """T4.2: ``create_speculative_proposal`` persists ``sourced_from``
    into ``expected_impact_json`` so the /api/proposals route can later
    surface it as ``cited_sources``.
    """
    from argosy.orchestrator.proposal_lifecycle import create_speculative_proposal

    _seed_user(client_with_db)
    SessionLocal = client_with_db.app.state.session_factory
    sess = SessionLocal()
    try:
        row = create_speculative_proposal(
            session=sess,
            user_id="ariel",
            ticker="HOOD",
            size_usd=800.0,
            sourced_from=["sentiment", "macro/yields"],
        )
        sess.commit()
        blob = json.loads(row.expected_impact_json)
        assert blob.get("sourced_from") == ["sentiment", "macro/yields"]
        # Existing fields must still be present.
        assert "exit_trigger" in blob
        assert "execution_mode" in blob
    finally:
        sess.close()
