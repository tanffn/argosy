"""Stance registry + /positions/thesis projection tests.

The /positions/thesis endpoint is a projection of the per-position STANCE
REGISTRY (argosy/services/position_stance.py): ONE canonical record per
position, precedence open proposal > verified review > plan. The plan-thesis
derivation stays cached on (plan_version, snapshot): a repeat request
recomputes nothing and writes no reliability-ledger row (perf fix)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import argosy.api.routes.positions as positions
import argosy.services.position_stance as ps
from argosy.services.per_position_thesis import PositionThesis
from argosy.state.models import Base, HoldingReview, Proposal, User


class _PV:
    id = 777
    decision_run_id = None


class _Snap:
    snapshot_date = "2026-06-12"
    positions = []
    total_usd_value_k = 1000.0


@pytest.fixture(autouse=True)
def _clear_thesis_cache():
    ps._THESIS_CACHE.clear()
    yield
    ps._THESIS_CACHE.clear()


def _session():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    return s


def _card(ticker: str, verdict: str = "HOLD", shares: float | None = 40.0,
          usd: float | None = 6000.0) -> PositionThesis:
    return PositionThesis(
        ticker=ticker,
        current_shares=shares,
        current_weight_pct=0.15,
        current_usd_value=usd,
        verdict=verdict,
        conviction="LOW",
        reasoning_md="No plan instruction found.",
    )


def _patch_plan_layer(monkeypatch, cards, derive_calls=None, emit_calls=None):
    def fake_derive(**kwargs):
        if derive_calls is not None:
            derive_calls.append(1)
        return list(cards)

    def fake_emit(*a, **k):
        if emit_calls is not None:
            emit_calls.append(1)

    monkeypatch.setattr(ps, "derive_position_theses", fake_derive)
    monkeypatch.setattr(ps, "emit_thesis_predictions", fake_emit)


def _sell_proposal(ticker="SPCX", size=40, status="awaiting_human", **kw):
    return Proposal(
        user_id="ariel", ticker=ticker, action="sell",
        size_shares_or_currency=size, size_units="shares",
        instrument="stock", order_type="market", tier="T2",
        account_class="main", status=status,
        rationale_summary="exit", shadow=0, **kw,
    )


# ---------------------------------------------------------------------------
# Route projection: cache + emit-once behavior (unchanged wire semantics)
# ---------------------------------------------------------------------------


def _route_setup(client_with_db, monkeypatch, derive_calls, emit_calls):
    monkeypatch.setattr(positions, "get_pending_draft", lambda db, uid: _PV())
    monkeypatch.setattr(positions, "get_current_plan", lambda db, uid: _PV())
    monkeypatch.setattr(
        positions, "_load_portfolio_snapshot", lambda uid, db=None: _Snap()
    )
    _patch_plan_layer(monkeypatch, [], derive_calls, emit_calls)
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
    finally:
        sess.close()


def test_thesis_endpoint_caches_and_emits_once(client_with_db, monkeypatch):
    derive_calls: list[int] = []
    emit_calls: list[int] = []
    _route_setup(client_with_db, monkeypatch, derive_calls, emit_calls)

    r1 = client_with_db.get("/api/positions/thesis", params={"user_id": "ariel"})
    r2 = client_with_db.get("/api/positions/thesis", params={"user_id": "ariel"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    # Second call served from cache: derive + ledger-emit each ran exactly once.
    assert len(derive_calls) == 1, f"derive ran {len(derive_calls)}x (expected 1)"
    assert len(emit_calls) == 1, f"emit ran {len(emit_calls)}x (expected 1; no write on cached read)"


def test_thesis_cache_misses_on_new_plan_version(client_with_db, monkeypatch):
    derive_calls: list[int] = []
    emit_calls: list[int] = []
    _route_setup(client_with_db, monkeypatch, derive_calls, emit_calls)
    client_with_db.get("/api/positions/thesis", params={"user_id": "ariel"})

    class _PV2:
        id = 888  # plan changed -> new key -> recompute
        decision_run_id = None
    monkeypatch.setattr(positions, "get_pending_draft", lambda db, uid: _PV2())
    client_with_db.get("/api/positions/thesis", params={"user_id": "ariel"})
    assert len(derive_calls) == 2  # recomputed for the new plan version


# ---------------------------------------------------------------------------
# Stance registry: proposal overlay semantics (moved from
# positions._overlay_open_proposals — same expectations)
# ---------------------------------------------------------------------------


def test_open_proposal_overlays_plan_verdict(monkeypatch):
    """An open trade proposal is FRESHER than the plan-derived stance: the
    stance must show it (SPCX HOLD-vs-SELL contradiction, 2026-07-10) — and
    the cached plan-layer card must NOT be mutated."""
    s = _session()
    s.add(_sell_proposal("SPCX", size=40))
    s.commit()

    card = _card("SPCX", "HOLD")
    _patch_plan_layer(monkeypatch, [card])
    rows = ps.rebuild_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    spcx = {r.symbol: r for r in rows}["SPCX"]
    assert spcx.stance == "SELL"  # 40 of 40 shares = full exit
    assert spcx.stance_source == "proposal"
    assert spcx.pending_proposal_id is not None
    assert "Pending decision" in spcx.reasoning_md
    assert card.verdict == "HOLD"  # original (cacheable) card untouched
    assert "Pending decision" not in card.reasoning_md
    s.close()


def test_partial_sell_is_trim(monkeypatch):
    s = _session()
    s.add(_sell_proposal("SPCX", size=10))  # 10 of 40 shares
    s.commit()
    _patch_plan_layer(monkeypatch, [_card("SPCX", "HOLD")])
    rows = ps.rebuild_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    assert rows[0].stance == "TRIM"
    s.close()


def test_cooling_proposal_included(monkeypatch):
    from datetime import datetime, timezone

    s = _session()
    s.add(_sell_proposal(
        "RKT", size=40, status="cooling",
        cooling_off_until=datetime(2026, 7, 16, tzinfo=timezone.utc),
    ))
    s.commit()
    _patch_plan_layer(monkeypatch, [_card("RKT", "HOLD")])
    rows = ps.rebuild_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    assert rows[0].stance == "SELL"
    assert rows[0].stance_source == "proposal"
    assert "resurfaces 2026-07-16" in rows[0].reasoning_md
    s.close()


# ---------------------------------------------------------------------------
# Stance registry: precedence + divergence
# ---------------------------------------------------------------------------


def test_precedence_proposal_beats_review_beats_plan(monkeypatch):
    s = _session()
    # SPCX: all three voices — the open proposal must win.
    s.add(_sell_proposal("SPCX", size=40))
    s.add(HoldingReview(
        user_id="ariel", symbol="SPCX", verdict="HOLD",
        confidence="high", reason="", outcome="hold",
    ))
    # META: verified review (no proposal) — review beats plan.
    s.add(HoldingReview(
        user_id="ariel", symbol="META", verdict="TRIM",
        confidence="medium", reason="", outcome="proposed",
    ))
    s.commit()
    _patch_plan_layer(
        monkeypatch,
        [_card("SPCX", "HOLD"), _card("META", "HOLD"), _card("NKE", "HOLD")],
    )
    rows = {r.symbol: r for r in ps.rebuild_stances(
        s, "ariel", plan_version=_PV(), snapshot=_Snap()
    )}
    assert rows["SPCX"].stance == "SELL"           # proposal > review('hold')
    assert rows["SPCX"].stance_source == "proposal"
    assert rows["SPCX"].review_verdict == "HOLD"   # ...but the review is recorded
    assert rows["META"].stance == "TRIM"           # verified review > plan
    assert rows["META"].stance_source == "review"
    assert rows["META"].conviction == "MED"
    assert rows["NKE"].stance == "HOLD"            # plan is the floor
    assert rows["NKE"].stance_source == "plan"
    assert rows["NKE"].plan_verdict == "HOLD"
    s.close()


def test_held_unverified_sets_divergence_not_stance(monkeypatch):
    """Fleet said act, blind gate failed → fail-closed: stance stays the
    plan's, divergence=True, nothing-hidden note present."""
    s = _session()
    s.add(HoldingReview(
        user_id="ariel", symbol="META", verdict="SELL",
        confidence="medium", reason="", outcome="held_unverified",
    ))
    s.commit()
    _patch_plan_layer(monkeypatch, [_card("META", "HOLD")])
    rows = ps.rebuild_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    meta = rows[0]
    assert meta.stance == "HOLD"  # NOT the review's SELL
    assert meta.stance_source == "plan"
    assert meta.divergence is True
    assert meta.review_verdict == "SELL"
    assert meta.review_outcome == "held_unverified"
    assert "blind verification diverged" in meta.reasoning_md
    s.close()


def test_underweight_note_on_plan_buy(monkeypatch):
    s = _session()
    _patch_plan_layer(monkeypatch, [_card("SPMV", "BUY")])
    rows = ps.rebuild_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    assert rows[0].stance == "BUY"
    assert "Underweight vs plan target" in rows[0].reasoning_md
    s.close()


def test_verified_hold_review_does_not_suppress_plan_buy(monkeypatch):
    """A review HOLD (outcome 'hold') answers "don't act on this holding" —
    it must NOT cancel the plan's deployment-schedule BUY (SPMV case)."""
    s = _session()
    s.add(HoldingReview(
        user_id="ariel", symbol="SPMV", verdict="HOLD",
        confidence="low", reason="", outcome="hold",
    ))
    s.commit()
    _patch_plan_layer(monkeypatch, [_card("SPMV", "BUY")])
    rows = ps.rebuild_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    spmv = rows[0]
    assert spmv.stance == "BUY"           # plan BUY survives
    assert spmv.stance_source == "plan"
    assert spmv.review_verdict == "HOLD"  # ...review still recorded
    assert spmv.review_outcome == "hold"
    assert "Underweight vs plan target" in spmv.reasoning_md
    s.close()


# ---------------------------------------------------------------------------
# get_stances staleness: a new source row forces a rebuild
# ---------------------------------------------------------------------------


def test_get_stances_rebuilds_when_new_proposal_lands(monkeypatch):
    s = _session()
    _patch_plan_layer(monkeypatch, [_card("SPCX", "HOLD")])

    first = ps.get_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    assert first[0].stance == "HOLD"

    # A new proposal row lands AFTER the registry was built → next read
    # must rebuild and show the proposal's stance.
    s.add(_sell_proposal("SPCX", size=40))
    s.commit()
    second = ps.get_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    assert second[0].stance == "SELL"
    assert second[0].stance_source == "proposal"
    s.close()


def test_get_stances_serves_stored_rows_when_fresh(monkeypatch):
    s = _session()
    derive_calls: list[int] = []
    _patch_plan_layer(monkeypatch, [_card("SPCX", "HOLD")], derive_calls)

    ps.get_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    ps.get_stances(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    # One derivation: the second read found fresh rows + unchanged sources.
    assert len(derive_calls) == 1
    s.close()


def test_projection_maps_stance_onto_dto(monkeypatch):
    """project_thesis_dtos keeps the wire shape and carries the stance's
    verdict + layered reasoning."""
    s = _session()
    s.add(_sell_proposal("SPCX", size=40))
    s.commit()
    _patch_plan_layer(monkeypatch, [_card("SPCX", "HOLD"), _card("AMD", "HOLD")])
    dtos = ps.project_thesis_dtos(s, "ariel", plan_version=_PV(), snapshot=_Snap())
    by = {d["ticker"]: d for d in dtos}
    assert by["SPCX"]["verdict"] == "SELL"
    assert "Pending decision" in by["SPCX"]["reasoning_md"]
    assert by["AMD"]["verdict"] == "HOLD"
    # Wire DTO validates unchanged.
    for d in dtos:
        positions.PositionThesisDTO(**d)
    s.close()
