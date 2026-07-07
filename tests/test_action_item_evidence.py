"""Tests for the deterministic action-item execution-evidence checker.

Covers the three contract points:
  * evidence-match — an overdue item whose execution shows in the book
    is stamped ``argosy_verified=True / looks_executed`` and DEMOTED
    from the OVERDUE count;
  * no-evidence-stays-overdue — without book evidence the item keeps
    nagging exactly as before;
  * confirmed-disappears — after the client confirms (existing ack
    endpoint) the greeting's needs-confirm entry is gone.

The two live shapes (plan v67) drive the fixtures: "Sell the June 17,
2026 net-vested NVDA shares and park the proceeds in SGOV" (due
2026-06-17) and "First UCITS dollar-cost-averaging tranche" (due
2026-07-01), executed by the Jul-6 deploy.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from argosy.services.action_item_evidence import (
    ActionEvidenceContext,
    looks_executed_unconfirmed_items,
)
from argosy.state.models import PlanVersion, PortfolioSnapshotRow, User

# ---------------------------------------------------------------------------
# Pure-context unit tests
# ---------------------------------------------------------------------------

SGOV_LABEL = (
    "Sell the June 17, 2026 net-vested NVDA shares and park the proceeds "
    "in SGOV pending UCITS deployment (90-day cap)"
)
UCITS_LABEL = "First UCITS dollar-cost-averaging tranche"


def _ctx(
    *,
    positions: list[dict] | None = None,
    ucits_symbols: frozenset[str] = frozenset({"CSPX", "EXUS", "FUSA", "IWQU", "EIMI"}),
    deploy_dates: list[date] | None = None,
    fills: list | None = None,
) -> ActionEvidenceContext:
    return ActionEvidenceContext(
        positions=positions or [],
        snapshot_date=date(2026, 7, 7),
        ucits_symbols=ucits_symbols,
        deploy_snapshot_dates=deploy_dates or [],
        fills=fills or [],
    )


def _pos(symbol: str, usd_value_k: float, location: str = "Leumi") -> dict:
    return {
        "symbol": symbol,
        "usd_value_k": usd_value_k,
        "location": location,
        "currency": "USD",
        "asset_type": "ETF",
    }


def test_sgov_evidence_matches_at_scale():
    ctx = _ctx(positions=[_pos("SGOV", 85.37), _pos("SGOV", 20.09, "schwab 876")])
    ev = ctx.evidence_for(label=SGOV_LABEL, detail="", dated=date(2026, 6, 17))
    assert ev is not None
    assert ev.status == "looks_executed"
    assert "SGOV" in ev.summary
    assert "$105.5k" in ev.summary
    assert "2 account(s)" in ev.summary


def test_sgov_dust_is_not_evidence():
    ctx = _ctx(positions=[_pos("SGOV", 0.2)])
    assert ctx.evidence_for(label=SGOV_LABEL, detail="", dated=date(2026, 6, 17)) is None


def test_ucits_tranche_evidence_needs_deploy_applied():
    held = [_pos("CSPX", 194.6), _pos("EXUS", 36.4), _pos("FUSA", 13.0)]
    # UCITS held but NO deploy applied on/after the due date -> no evidence
    # (pre-existing positions alone don't satisfy the item).
    ctx = _ctx(positions=held, deploy_dates=[])
    assert ctx.evidence_for(label=UCITS_LABEL, detail="", dated=date(2026, 7, 1)) is None
    # Deploy applied Jul-6 >= due Jul-1 -> evidence.
    ctx = _ctx(positions=held, deploy_dates=[date(2026, 7, 6)])
    ev = ctx.evidence_for(label=UCITS_LABEL, detail="", dated=date(2026, 7, 1))
    assert ev is not None
    assert "CSPX" in ev.summary and "EXUS" in ev.summary
    assert ev.status == "looks_executed"
    # Deploy applied BEFORE the due date only -> not this tranche.
    ctx = _ctx(positions=held, deploy_dates=[date(2026, 6, 20)])
    assert ctx.evidence_for(label=UCITS_LABEL, detail="", dated=date(2026, 7, 1)) is None


def test_ucits_needs_min_distinct_holdings():
    ctx = _ctx(
        positions=[_pos("CSPX", 194.6), _pos("EXUS", 36.4)],
        deploy_dates=[date(2026, 7, 6)],
    )
    assert ctx.evidence_for(label=UCITS_LABEL, detail="", dated=date(2026, 7, 1)) is None


def test_unrelated_item_gets_no_evidence():
    ctx = _ctx(positions=[_pos("SGOV", 105.0)], deploy_dates=[date(2026, 7, 6)])
    assert (
        ctx.evidence_for(
            label="Engage cross-border counsel", detail="", dated=date(2026, 7, 4)
        )
        is None
    )


# ---------------------------------------------------------------------------
# Endpoint integration — evidence-match / stays-overdue / confirmed-disappears
# ---------------------------------------------------------------------------


def _seed_user(client_with_db, user_id: str = "ariel") -> None:
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, user_id) is None:
            sess.add(User(id=user_id, plan="free"))
            sess.commit()
    finally:
        sess.close()


def _seed_current_plan_with_items(client_with_db) -> int:
    _seed_user(client_with_db)
    short = {
        "horizon": "short",
        "freshness_expected": "monthly",
        "status": "major_revision",
        "posture": "test",
        "targets": [],
        "themes": [],
        "actions": [
            {
                "label": SGOV_LABEL,
                "detail": "Park net vest proceeds in SGOV.",
                "trigger_or_date": "2026-06-17 vest",
                "horizon_kind": "dated",
                "rationale": "",
                "cited_sources": [],
            },
            {
                "label": UCITS_LABEL,
                "detail": "Deploy the first tranche into CSPX/EXUS per plan.",
                "trigger_or_date": "2026-07-01",
                "horizon_kind": "dated",
                "rationale": "",
                "cited_sources": [],
            },
        ],
        "deltas_from_prior": [],
        "rationale": "",
        "cited_sources": [],
    }
    sess = client_with_db.app.state.session_factory()
    try:
        pv = PlanVersion(
            user_id="ariel",
            role="current",
            version_label="test-current",
            raw_markdown="",
            horizon_short_json=json.dumps(short),
            accepted_at=datetime.now(timezone.utc),
        )
        sess.add(pv)
        sess.commit()
        sess.refresh(pv)
        return pv.id
    finally:
        sess.close()


def _seed_book_with_deploy(client_with_db) -> None:
    """Latest snapshot holding SGOV at scale + 3 plan UCITS names; the
    snapshot itself is a fills-applied deploy row (Jul-6)."""
    positions = [
        _pos("SGOV", 85.37),
        _pos("SGOV", 20.09, "schwab 876"),
        _pos("CSPX", 194.6),
        _pos("EXUS", 36.4),
        _pos("FUSA", 13.0),
    ]
    sess = client_with_db.app.state.session_factory()
    try:
        sess.add(
            PortfolioSnapshotRow(
                user_id="ariel",
                snapshot_date=date(2026, 7, 6),
                imported_at=datetime.now(timezone.utc).replace(tzinfo=None),
                source_path="fills-applied:2026-07-06-deploy",
                positions_json=json.dumps(positions),
                totals_json=json.dumps({"total_usd_value_k": 3993.9}),
            )
        )
        sess.commit()
    finally:
        sess.close()


def _seed_ucits_plan_doc(client_with_db, pv_id: int) -> None:
    """Stamp a minimal TargetAllocationDoc with IE-domiciled instruments so
    the evidence context resolves the plan's UCITS symbols."""
    doc = {
        "schema_version": 1,
        "anchor_sigma": 0.18,
        "blended_sigma": 0.18,
        "nvda_cap_pct": 13.0,
        "fi_pct": 9.0,
        "provenance": "test",
        "glide": [],
        "classes": [
            {
                "label": "US core",
                "snapshot_category": "Core Equity",
                "sigma_class": "us_equity",
                "target_pct": 50.0,
                "instruments": [
                    {"symbol": "CSPX", "role": "primary", "weight_within_class_pct": 100.0, "domicile": "IE"},
                ],
            },
            {
                "label": "Intl",
                "snapshot_category": "International",
                "sigma_class": "intl_equity",
                "target_pct": 30.0,
                "instruments": [
                    {"symbol": "EXUS", "role": "primary", "weight_within_class_pct": 100.0, "domicile": "IE"},
                ],
            },
            {
                "label": "Dividend",
                "snapshot_category": "Dividend",
                "sigma_class": "us_equity",
                "target_pct": 20.0,
                "instruments": [
                    {"symbol": "FUSA", "role": "primary", "weight_within_class_pct": 100.0, "domicile": "IE"},
                ],
            },
        ],
    }
    sess = client_with_db.app.state.session_factory()
    try:
        pv = sess.get(PlanVersion, pv_id)
        pv.target_allocation_json = json.dumps(doc)
        sess.commit()
    finally:
        sess.close()


def test_evidence_match_verifies_and_demotes_overdue(client_with_db):
    pv_id = _seed_current_plan_with_items(client_with_db)
    _seed_ucits_plan_doc(client_with_db, pv_id)
    _seed_book_with_deploy(client_with_db)

    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    by_label = {it["label"]: it for it in body["items"]}
    sgov = by_label[SGOV_LABEL]
    ucits = by_label[UCITS_LABEL]

    for it in (sgov, ucits):
        assert it["status"] == "OVERDUE"  # the date is still past…
        assert it["argosy_verified"] is True  # …but Argosy found the evidence
        assert it["argosy_verified_status"] == "looks_executed"
        assert it["argosy_verified_summary"]
    # …and the OVERDUE badge no longer nags for verified items.
    assert body["overdue_count"] == 0


def test_no_evidence_stays_overdue(client_with_db):
    _seed_current_plan_with_items(client_with_db)
    # A book with NO SGOV / UCITS positions and no deploy rows.
    sess = client_with_db.app.state.session_factory()
    try:
        sess.add(
            PortfolioSnapshotRow(
                user_id="ariel",
                snapshot_date=date(2026, 7, 6),
                imported_at=datetime.now(timezone.utc).replace(tzinfo=None),
                source_path="tsv",
                positions_json=json.dumps([_pos("NVDA", 2243.2)]),
                totals_json="{}",
            )
        )
        sess.commit()
    finally:
        sess.close()

    body = client_with_db.get("/api/plan/action-items?user_id=ariel").json()
    for it in body["items"]:
        assert it["argosy_verified"] is None
    assert body["overdue_count"] == 2


def test_confirmed_item_disappears_from_greeting(client_with_db):
    pv_id = _seed_current_plan_with_items(client_with_db)
    _seed_ucits_plan_doc(client_with_db, pv_id)
    _seed_book_with_deploy(client_with_db)

    SF = client_with_db.app.state.session_factory
    with SF() as s:
        items = looks_executed_unconfirmed_items(s, "ariel")
    assert {it.label for it in items} == {SGOV_LABEL, UCITS_LABEL}

    # The greeting surfaces both as needs-confirm with the ack payload.
    r = client_with_db.get("/api/home/greeting?user_id=ariel")
    assert r.status_code == 200
    confirms = [
        i for i in r.json()["needs_you"] if i["kind"] == "action_item_confirm"
    ]
    assert len(confirms) == 2
    for c in confirms:
        assert c["headline"].startswith("Looks executed — confirm:")
        assert c["why_md"]
        assert c["tone"] == "confirm"
        assert c["ack"]["method"] == "POST"
        assert c["ack"]["endpoint"].startswith("/api/plan/action-items/")
        assert c["ack"]["content_fingerprint"]

    # Client confirms the SGOV item through the EXISTING ack endpoint.
    sgov_item = next(it for it in items if it.label == SGOV_LABEL)
    ack = client_with_db.post(
        f"/api/plan/action-items/{sgov_item.item_id}/ack",
        json={
            "user_id": "ariel",
            "content_fingerprint": sgov_item.content_fingerprint,
        },
    )
    assert ack.status_code == 200

    # The confirmed item disappears; the unconfirmed one remains.
    r2 = client_with_db.get("/api/home/greeting?user_id=ariel")
    confirms2 = [
        i for i in r2.json()["needs_you"] if i["kind"] == "action_item_confirm"
    ]
    assert len(confirms2) == 1
    assert UCITS_LABEL[:30] in confirms2[0]["headline"]
