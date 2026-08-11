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
