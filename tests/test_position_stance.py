"""Position-stance registry — phantom ADD-ticker gate.

Prose acronyms in plan horizons (IPS, UCITS, TIPS, FIRE, …) match the
ticker regex and used to become stance rows. The universe gate in
``derive_position_theses`` (held ∪ plan-named ∪ instrument_reference)
must drop them before ``rebuild_stances`` persists a row.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from argosy.services.per_position_thesis import derive_position_theses
from argosy.services.position_stance import rebuild_stances
from argosy.state.models import PositionStance


_PHANTOM_ACRONYMS = (
    "CGT", "D1", "FI", "FIRE", "IG", "IPS", "REIT", "T", "TIPS", "UCITS", "YTD",
)


def _make_horizon(horizon: str, actions: list[dict] | None = None) -> str:
    return json.dumps({
        "horizon": horizon,
        "freshness_expected": "monthly",
        "status": "minor_revision",
        "posture": "test",
        "targets": [],
        "themes": [],
        "actions": actions or [],
        "speculative_candidates": [],
        "deltas_from_prior": [],
        "rationale": "",
        "cited_sources": [],
    })


def test_prose_acronyms_do_not_create_thesis_cards():
    """Plan prose naming IPS / UCITS / TIPS / FIRE must not mint ADD cards."""
    positions = [
        {"symbol": "NVDA", "shares": 100.0, "usd_value_k": 500.0},
        {"symbol": "CSPX", "shares": 10.0, "usd_value_k": 50.0},
    ]
    horizon = _make_horizon(
        "medium",
        actions=[{
            "label": "Reaffirm IPS glide; prefer UCITS wrappers over US-situs",
            "detail": (
                "Keep TIPS / FIRE runway intact; IG credit and REIT sleeves "
                "stay estate-safe. YTD CGT budget and FI allocation unchanged."
            ),
            "rationale": (
                "Investment Policy Statement (IPS) + UCITS domicile rule; "
                "TIPS as ballast; FIRE horizon unchanged."
            ),
        }],
    )
    pv = SimpleNamespace(
        horizon_short_json=None,
        horizon_medium_json=horizon,
        horizon_long_json=None,
    )
    snap = SimpleNamespace(positions=positions, total_usd_value_k=550.0)
    out = derive_position_theses(pv, snap, [])

    tickers = {c.ticker for c in out}
    for phantom in ("IPS", "UCITS", "TIPS", "FIRE", "IG", "REIT", "YTD", "CGT", "FI"):
        assert phantom not in tickers, f"phantom ADD card for {phantom}: {tickers}"

    # Held positions still get cards.
    assert "NVDA" in tickers
    assert "CSPX" in tickers
    assert all(c.verdict != "ADD" or c.ticker in {"NVDA", "CSPX"} for c in out)
    assert not any(c.verdict == "ADD" for c in out)


def test_plan_named_symbol_still_creates_add_card():
    """A real instrument named in allowed_symbols (plan list) still ADDs."""
    positions = [{"symbol": "NVDA", "shares": 10.0, "usd_value_k": 100.0}]
    horizon = _make_horizon(
        "short",
        actions=[{
            "label": "Open XEON UCITS cash sleeve",
            "detail": "Seed XEON for euro cash; IPS cash floor unchanged.",
            "rationale": "UCITS cash equivalent vs SGOV.",
        }],
    )
    pv = SimpleNamespace(
        horizon_short_json=horizon,
        horizon_medium_json=None,
        horizon_long_json=None,
    )
    snap = SimpleNamespace(positions=positions, total_usd_value_k=100.0)
    out = derive_position_theses(pv, snap, [], allowed_symbols={"XEON"})
    tickers = {c.ticker for c in out}
    assert "XEON" in tickers
    assert "IPS" not in tickers
    assert "UCITS" not in tickers
    xeon = next(c for c in out if c.ticker == "XEON")
    assert xeon.verdict == "ADD"


def test_rebuild_stances_drops_phantom_rows(monkeypatch, tmp_path):
    """rebuild_stances persists only universe-valid thesis cards."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base, User

    engine = create_engine(f"sqlite:///{tmp_path / 'stance_phantoms.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(User(id="ariel", plan="free"))
    s.commit()

    # Simulate pre-fix DB pollution: phantom rows already present.
    for sym in ("IPS", "UCITS", "FIRE", "NVDA"):
        s.add(PositionStance(
            user_id="ariel",
            symbol=sym,
            stance="HOLD" if sym == "NVDA" else "ADD",
            stance_source="plan",
            plan_verdict="HOLD" if sym == "NVDA" else "ADD",
            conviction="LOW",
            reasoning_md="",
            divergence=False,
        ))
    s.commit()

    from argosy.services.per_position_thesis import PositionThesis

    real_cards = [
        PositionThesis(
            ticker="NVDA",
            current_shares=100.0,
            current_weight_pct=90.0,
            current_usd_value=90000.0,
            verdict="HOLD",
            conviction="HIGH",
            reasoning_md="held",
            cited_sources=[],
            target_weight_pct=None,
            target_shares=None,
        ),
        PositionThesis(
            ticker="CSPX",
            current_shares=None,
            current_weight_pct=None,
            current_usd_value=None,
            verdict="ADD",
            conviction="MEDIUM",
            reasoning_md="plan should-add",
            cited_sources=[],
            target_weight_pct=None,
            target_shares=None,
        ),
    ]
    monkeypatch.setattr(
        "argosy.services.position_stance._plan_theses",
        lambda *a, **k: real_cards,
    )
    pv = SimpleNamespace(id=1, decision_run_id=None)
    snap = SimpleNamespace(
        positions=[{"symbol": "NVDA", "shares": 100, "usd_value_k": 90}],
        as_of=None,
    )
    rows = rebuild_stances(s, "ariel", plan_version=pv, snapshot=snap)
    symbols = {r.symbol for r in rows}
    assert symbols == {"NVDA", "CSPX"}
    for phantom in _PHANTOM_ACRONYMS:
        assert phantom not in symbols

    persisted = {
        r.symbol
        for r in s.query(PositionStance).filter_by(user_id="ariel").all()
    }
    assert persisted == {"NVDA", "CSPX"}
    s.close()


# --------------------------------------------------------------------------- #
# Divergence flagging (SEAM 2) — verdict/review-vs-plan conflicts are FLAGGED,
# never silently False, and NEVER change which stance wins.
# --------------------------------------------------------------------------- #


def _divergence_session(tmp_path, name):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base, User

    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    return s


def _card(ticker, verdict, *, shares=100.0):
    from argosy.services.per_position_thesis import PositionThesis

    return PositionThesis(
        ticker=ticker,
        current_shares=shares,
        current_weight_pct=90.0,
        current_usd_value=90000.0,
        verdict=verdict,
        conviction="HIGH",
        reasoning_md="held",
        cited_sources=[],
        target_weight_pct=None,
        target_shares=None,
    )


def _rebuild(s, cards, monkeypatch):
    monkeypatch.setattr(
        "argosy.services.position_stance._plan_theses", lambda *a, **k: cards
    )
    pv = SimpleNamespace(id=1, decision_run_id=None)
    snap = SimpleNamespace(
        positions=[{"symbol": "NVDA", "shares": 100, "usd_value_k": 90}], as_of=None
    )
    return {r.symbol: r for r in rebuild_stances(s, "ariel", plan_version=pv, snapshot=snap)}


def test_review_hold_vs_plan_sell_flags_divergence(monkeypatch, tmp_path):
    """(i) A verified HOLD review against a plan SELL now sets divergence=True
    (was silently False) — the stance shown STAYS the plan's SELL."""
    from datetime import datetime, timezone

    from argosy.state.models import HoldingReview

    s = _divergence_session(tmp_path, "div_review.db")
    s.add(HoldingReview(
        user_id="ariel", symbol="NVDA", verdict="HOLD", confidence="HIGH",
        outcome="hold", reviewed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        reason="",
    ))
    s.commit()

    rows = _rebuild(s, [_card("NVDA", "SELL")], monkeypatch)
    nvda = rows["NVDA"]
    assert nvda.stance == "SELL"  # stance unchanged — plan wins
    assert nvda.divergence is True
    assert "contradicts the plan" in nvda.reasoning_md
    s.close()


def test_settled_verdict_hold_vs_plan_sell_flags_divergence(monkeypatch, tmp_path):
    """(ii) A DEFENDED settled Verdict=HOLD (no review) against a plan SELL is
    flagged via the batched provenance/verdict read — stance stays SELL."""
    from datetime import datetime, timezone

    from argosy.state.models import Verdict

    s = _divergence_session(tmp_path, "div_verdict.db")
    s.add(Verdict(
        user_id="ariel", subject="NVDA", verdict="HOLD", conviction="HIGH",
        settled=True, created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    ))
    s.commit()

    rows = _rebuild(s, [_card("NVDA", "SELL")], monkeypatch)
    nvda = rows["NVDA"]
    assert nvda.stance == "SELL"
    assert nvda.divergence is True
    assert "settled fleet verdict" in nvda.reasoning_md.lower()
    s.close()


def test_spmv_hold_vs_hold_no_divergence(monkeypatch, tmp_path):
    """SPMV carve-out untouched: a routine HOLD review over a plan HOLD overrides
    without flagging divergence."""
    from datetime import datetime, timezone

    from argosy.state.models import HoldingReview

    s = _divergence_session(tmp_path, "div_spmv.db")
    s.add(HoldingReview(
        user_id="ariel", symbol="NVDA", verdict="HOLD", confidence="HIGH",
        outcome="hold", reviewed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        reason="",
    ))
    s.commit()

    rows = _rebuild(s, [_card("NVDA", "HOLD")], monkeypatch)
    nvda = rows["NVDA"]
    assert nvda.stance == "HOLD"
    assert nvda.divergence is False
    s.close()


def test_buy_underweight_no_divergence(monkeypatch, tmp_path):
    """A plan ADD (underweight) with no settled verdict stays divergence=False."""
    s = _divergence_session(tmp_path, "div_buy.db")
    rows = _rebuild(s, [_card("NVDA", "ADD")], monkeypatch)
    nvda = rows["NVDA"]
    assert nvda.stance == "ADD"
    assert nvda.divergence is False
    s.close()


def test_revision_proposed_still_sets_divergence(monkeypatch, tmp_path):
    """Phase-2 branch untouched: revision_proposed still flags divergence and
    keeps the plan SELL."""
    from datetime import datetime, timezone

    from argosy.state.models import HoldingReview

    s = _divergence_session(tmp_path, "div_phase2.db")
    s.add(HoldingReview(
        user_id="ariel", symbol="NVDA", verdict="HOLD", confidence="HIGH",
        outcome="revision_proposed",
        reviewed_at=datetime(2026, 7, 1, tzinfo=timezone.utc), reason="",
    ))
    s.commit()

    rows = _rebuild(s, [_card("NVDA", "SELL")], monkeypatch)
    nvda = rows["NVDA"]
    assert nvda.stance == "SELL"
    assert nvda.divergence is True
    assert "stance revision proposed" in nvda.reasoning_md.lower()
    s.close()


# --------------------------------------------------------------------------- #
# FIX 1 — cash-sentinel "-" filter                                            #
# --------------------------------------------------------------------------- #


def test_ticker_to_position_drops_dash_sentinel():
    """_ticker_to_position must skip the '-' cash-sentinel symbol."""
    from argosy.services.per_position_thesis import _ticker_to_position

    positions = [
        {"symbol": "-", "shares": 5896.0, "usd_value_k": 74.9},
        {"symbol": "NVDA", "shares": 100.0, "usd_value_k": 500.0},
        {"symbol": "", "shares": 10.0, "usd_value_k": 1.0},
    ]
    result = _ticker_to_position(positions)
    assert "-" not in result, "'-' sentinel must be filtered"
    assert "" not in result, "empty symbol must be filtered"
    assert "NVDA" in result


def test_project_thesis_dtos_drops_dash_sentinel(monkeypatch):
    """project_thesis_dtos must drop any card whose ticker is '-'."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.services.per_position_thesis import PositionThesis
    from argosy.services.position_stance import project_thesis_dtos
    from argosy.state.models import Base, User

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()

    # Simulate a "-" card slipping through (historical data / old snapshot)
    dash_card = PositionThesis(
        ticker="-",
        current_shares=5896.0,
        current_weight_pct=1.79,
        current_usd_value=74893.0,
        verdict="HOLD",
        conviction="MED",
        reasoning_md="",
        cited_sources=[],
        target_weight_pct=None,
        target_shares=None,
    )
    nvda_card = PositionThesis(
        ticker="NVDA",
        current_shares=100.0,
        current_weight_pct=90.0,
        current_usd_value=90000.0,
        verdict="HOLD",
        conviction="HIGH",
        reasoning_md="Strong thesis.",
        cited_sources=[],
        target_weight_pct=None,
        target_shares=None,
    )

    pv = SimpleNamespace(id=42, decision_run_id=None)
    snap = SimpleNamespace(
        snapshot_date="2026-08-01",
        positions=[{"symbol": "NVDA", "shares": 100, "usd_value_k": 90}],
        total_usd_value_k=90.0,
        as_of=None,
    )

    monkeypatch.setattr(
        "argosy.services.position_stance._plan_theses",
        lambda *a, **k: [dash_card, nvda_card],
    )
    monkeypatch.setattr(
        "argosy.services.verdict_registry.provenance_for_subjects",
        lambda *a, **k: {},
    )

    dtos = project_thesis_dtos(s, "ariel", plan_version=pv, snapshot=snap)
    tickers = [d["ticker"] for d in dtos]
    assert "-" not in tickers, "'-' sentinel must not appear in the DTO projection"
    assert "NVDA" in tickers
    s.close()


# --------------------------------------------------------------------------- #
# FIX 2 — analysis_state derivation                                           #
# --------------------------------------------------------------------------- #


def test_analysis_state_unreviewed(monkeypatch):
    """Empty reasoning AND no falsifiers → 'unreviewed'."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.services.per_position_thesis import PositionThesis
    from argosy.services.position_stance import project_thesis_dtos
    from argosy.state.models import Base, User

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()

    card = PositionThesis(
        ticker="AMZN",
        current_shares=10.0,
        current_weight_pct=5.0,
        current_usd_value=20000.0,
        verdict="HOLD",
        conviction="LOW",
        reasoning_md="",      # blank
        cited_sources=[],
        target_weight_pct=None,
        target_shares=None,
    )
    pv = SimpleNamespace(id=99, decision_run_id=None)
    snap = SimpleNamespace(
        snapshot_date="2026-08-01",
        positions=[{"symbol": "AMZN", "shares": 10, "usd_value_k": 20}],
        total_usd_value_k=20.0,
        as_of=None,
    )
    monkeypatch.setattr(
        "argosy.services.position_stance._plan_theses", lambda *a, **k: [card]
    )
    # No falsifiers from provenance
    monkeypatch.setattr(
        "argosy.services.verdict_registry.provenance_for_subjects",
        lambda *a, **k: {},
    )
    dtos = project_thesis_dtos(s, "ariel", plan_version=pv, snapshot=snap)
    assert len(dtos) == 1
    assert dtos[0]["analysis_state"] == "unreviewed"
    s.close()


def test_analysis_state_thin_low_conviction(monkeypatch):
    """Non-empty reasoning + falsifiers but LOW conviction → 'thin'."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.services.per_position_thesis import PositionThesis
    from argosy.services.position_stance import project_thesis_dtos
    from argosy.state.models import Base, User
    from argosy.services.verdict_registry import VerdictProvenance

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()

    card = PositionThesis(
        ticker="GOOG",
        current_shares=5.0,
        current_weight_pct=2.0,
        current_usd_value=8000.0,
        verdict="HOLD",
        conviction="LOW",       # LOW conviction → thin
        reasoning_md="Some rationale.",
        cited_sources=[],
        target_weight_pct=None,
        target_shares=None,
    )
    pv = SimpleNamespace(id=100, decision_run_id=None)
    snap = SimpleNamespace(
        snapshot_date="2026-08-01",
        positions=[{"symbol": "GOOG", "shares": 5, "usd_value_k": 8}],
        total_usd_value_k=8.0,
        as_of=None,
    )
    monkeypatch.setattr(
        "argosy.services.position_stance._plan_theses", lambda *a, **k: [card]
    )
    monkeypatch.setattr(
        "argosy.services.verdict_registry.provenance_for_subjects",
        lambda *a, **k: {
            "GOOG": VerdictProvenance(
                falsifier_state="armed",
                falsifiers=("GOOG drops cloud market share below 20%",),
                next_validation=None,
                last_fleet_check_at=None,
                reasoning_md="Some rationale.",
            )
        },
    )
    dtos = project_thesis_dtos(s, "ariel", plan_version=pv, snapshot=snap)
    assert len(dtos) == 1
    assert dtos[0]["analysis_state"] == "thin"
    s.close()


def test_analysis_state_analysed(monkeypatch):
    """MED/HIGH conviction + non-empty reasoning + falsifiers → 'analysed'."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.services.per_position_thesis import PositionThesis
    from argosy.services.position_stance import project_thesis_dtos
    from argosy.state.models import Base, User
    from argosy.services.verdict_registry import VerdictProvenance

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()

    card = PositionThesis(
        ticker="NVDA",
        current_shares=100.0,
        current_weight_pct=45.0,
        current_usd_value=500000.0,
        verdict="HOLD",
        conviction="HIGH",
        reasoning_md="Dominant AI accelerator.",
        cited_sources=[],
        target_weight_pct=None,
        target_shares=None,
    )
    pv = SimpleNamespace(id=101, decision_run_id=None)
    snap = SimpleNamespace(
        snapshot_date="2026-08-01",
        positions=[{"symbol": "NVDA", "shares": 100, "usd_value_k": 500}],
        total_usd_value_k=500.0,
        as_of=None,
    )
    monkeypatch.setattr(
        "argosy.services.position_stance._plan_theses", lambda *a, **k: [card]
    )
    monkeypatch.setattr(
        "argosy.services.verdict_registry.provenance_for_subjects",
        lambda *a, **k: {
            "NVDA": VerdictProvenance(
                falsifier_state="armed",
                falsifiers=("AMD closes H100 performance gap",),
                next_validation="2026-09-01",
                last_fleet_check_at="2026-08-01T00:00:00Z",
                reasoning_md="Dominant AI accelerator.",
            )
        },
    )
    dtos = project_thesis_dtos(s, "ariel", plan_version=pv, snapshot=snap)
    assert len(dtos) == 1
    assert dtos[0]["analysis_state"] == "analysed"
    s.close()


def test_analysis_state_thin_no_falsifiers_despite_med_conviction(monkeypatch):
    """MED conviction + non-empty reasoning but NO falsifiers → still 'thin'.
    Falsifiers are the minimum honesty bar for 'analysed'."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.services.per_position_thesis import PositionThesis
    from argosy.services.position_stance import project_thesis_dtos
    from argosy.state.models import Base, User

    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(User(id="ariel", plan="free"))
    s.commit()

    card = PositionThesis(
        ticker="META",
        current_shares=20.0,
        current_weight_pct=10.0,
        current_usd_value=15000.0,
        verdict="HOLD",
        conviction="MED",      # MED conviction, but...
        reasoning_md="Solid ad revenue.",
        cited_sources=[],
        target_weight_pct=None,
        target_shares=None,
    )
    pv = SimpleNamespace(id=102, decision_run_id=None)
    snap = SimpleNamespace(
        snapshot_date="2026-08-01",
        positions=[{"symbol": "META", "shares": 20, "usd_value_k": 15}],
        total_usd_value_k=15.0,
        as_of=None,
    )
    monkeypatch.setattr(
        "argosy.services.position_stance._plan_theses", lambda *a, **k: [card]
    )
    # No falsifiers from provenance at all
    monkeypatch.setattr(
        "argosy.services.verdict_registry.provenance_for_subjects",
        lambda *a, **k: {},
    )
    dtos = project_thesis_dtos(s, "ariel", plan_version=pv, snapshot=snap)
    assert len(dtos) == 1
    assert dtos[0]["analysis_state"] == "thin", (
        "MED conviction without falsifiers must be 'thin', not 'analysed'"
    )
