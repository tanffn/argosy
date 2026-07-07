"""Tests for the pure holistic-rebalance composition function.

Covers the thesis-gating rules (a)-(c) + the critical-drift override, the
fund-this-from-that pairing, the estate gate on the buy side, and the
taxable-event note on every sell leg. The pure function takes plain inputs
(real ``TargetAllocationDoc`` for the estate gate; dataclass/SimpleNamespace
stand-ins for verdicts + alerts) so it stays deterministic and accessor-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from argosy.services.holistic_rebalance_review import (
    RebalanceReview,
    compose_rebalance_review,
)
from argosy.services.per_position_thesis import PositionThesis
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    TargetAllocationDoc,
)


# --- input builders ----------------------------------------------------------


def _val(v: float) -> SimpleNamespace:
    """Mimic ValueWithRationale's ``.value`` accessor the composer reads."""
    return SimpleNamespace(value=v)


def _alert(asset_class: str, drift_pp: float, rule_fired: str = "5pp_threshold") -> SimpleNamespace:
    return SimpleNamespace(
        asset_class=asset_class,
        drift_pp=_val(drift_pp),
        rule_fired=rule_fired,
    )


def _thesis(
    ticker: str,
    *,
    verdict: str,
    conviction: str = "MEDIUM",
    current_weight_pct: float | None = 20.0,
    target_weight_pct: float | None = None,
    current_usd_value: float | None = 200_000.0,
) -> PositionThesis:
    return PositionThesis(
        ticker=ticker,
        current_shares=None,
        current_weight_pct=current_weight_pct,
        current_usd_value=current_usd_value,
        verdict=verdict,
        conviction=conviction,
        reasoning_md="",
        target_weight_pct=target_weight_pct,
    )


def _doc(*, equity_symbols: list[tuple[str, str | None]], bond_symbols: list[tuple[str, str | None]] | None = None) -> TargetAllocationDoc:
    """Build a minimal real TargetAllocationDoc.

    ``equity_symbols`` / ``bond_symbols`` are (symbol, domicile) tuples so a
    test can stamp a US-domiciled buy candidate and exercise the estate gate.
    """
    def _instr(sym: str, dom: str | None) -> AllocationInstrument:
        return AllocationInstrument(
            symbol=sym, role="primary", weight_within_class_pct=100.0, domicile=dom,
        )

    classes = [
        AllocationClassDoc(
            label="Equity core",
            snapshot_category="Core Equity",
            sigma_class="equity",
            target_pct=70.0,
            instruments=[_instr(s, d) for s, d in equity_symbols],
        ),
    ]
    if bond_symbols:
        classes.append(AllocationClassDoc(
            label="Bonds",
            snapshot_category="Cash",
            sigma_class="bonds",
            target_pct=30.0,
            instruments=[_instr(s, d) for s, d in bond_symbols],
        ))
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18,
        nvda_cap_pct=13.0, fi_pct=4.0, provenance="test", classes=classes, glide=[],
    )


# --- (a) high-conviction intact NOT trimmed under non-critical drift --------


def test_high_conviction_intact_not_trimmed_non_critical():
    doc = _doc(equity_symbols=[("NVDA", None)])
    verdicts = [
        _thesis("NVDA", verdict="HOLD", conviction="HIGH",
                current_weight_pct=20.0, target_weight_pct=None),
    ]
    alerts = [_alert("equity", drift_pp=6.0, rule_fired="5pp_threshold")]

    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=[], total_book_usd=1_000_000.0,
    )
    assert review.status == "ok"
    trims = [l for l in review.legs if l.action in ("TRIM", "SELL")]
    assert trims == []  # intact HIGH-conviction, non-critical -> held


# --- (b) IS trimmed under critical drift (25%-relative) ----------------------


def test_high_conviction_intact_trimmed_under_critical_drift():
    doc = _doc(equity_symbols=[("NVDA", None)])
    verdicts = [
        _thesis("NVDA", verdict="HOLD", conviction="HIGH",
                current_weight_pct=20.0, target_weight_pct=None),
    ]
    # 25%-relative rule -> critical override fires even for an intact position.
    alerts = [_alert("equity", drift_pp=6.0, rule_fired="25pct_relative")]

    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=[], total_book_usd=1_000_000.0,
    )
    trims = [l for l in review.legs if l.action in ("TRIM", "SELL")]
    assert len(trims) == 1
    assert trims[0].ticker == "NVDA"
    assert trims[0].gate_reason == "CRITICAL_DRIFT_OVERRIDE"


# --- (c) weakened-thesis position IS trimmed (non-critical drift) -----------


def test_weakened_thesis_position_is_trimmed():
    doc = _doc(equity_symbols=[("GOOG", None)])
    verdicts = [
        _thesis("GOOG", verdict="HOLD", conviction="HIGH",
                current_weight_pct=15.0, target_weight_pct=None),
    ]
    alerts = [_alert("equity", drift_pp=6.0, rule_fired="5pp_threshold")]
    flags = [{
        "kind": "thesis_monitor_weakened", "ticker": "GOOG",
        "severity": "warning", "dedup_key": "v1|thesis_monitor|ariel|GOOG|weakened",
    }]

    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=flags, total_book_usd=1_000_000.0,
    )
    trims = [l for l in review.legs if l.action in ("TRIM", "SELL")]
    assert len(trims) == 1
    assert trims[0].gate_reason == "THESIS_WEAKENED"
    assert "v1|thesis_monitor|ariel|GOOG|weakened" in trims[0].cited_flags


# --- (d) trims pair with under-target buys (fund-this-from-that) ------------


def test_trims_pair_with_under_target_buys():
    # Equity over-target (trim NVDA), bonds under-target (buy IBTA).
    doc = _doc(
        equity_symbols=[("NVDA", None)],
        bond_symbols=[("IBTA", "IE")],
    )
    verdicts = [
        _thesis("NVDA", verdict="TRIM", conviction="HIGH",
                current_weight_pct=30.0, target_weight_pct=20.0,
                current_usd_value=300_000.0),
        _thesis("IBTA", verdict="ADD", conviction="MEDIUM",
                current_weight_pct=0.0, target_weight_pct=None,
                current_usd_value=None),
    ]
    alerts = [
        _alert("equity", drift_pp=10.0, rule_fired="5pp_threshold"),
        _alert("bonds", drift_pp=-10.0, rule_fired="5pp_threshold"),
    ]

    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=[], total_book_usd=1_000_000.0,
    )
    trims = [l for l in review.legs if l.action in ("TRIM", "SELL")]
    buys = [l for l in review.legs if l.action == "BUY"]
    assert len(trims) == 1 and trims[0].ticker == "NVDA"
    assert len(buys) == 1 and buys[0].ticker == "IBTA"
    assert buys[0].asset_class == "bonds"
    # Buy is funded from trim proceeds — net cash near neutral, not a fresh outlay.
    assert review.net_cash_delta_usd <= 0.0
    assert abs(review.net_cash_delta_usd) <= trims[0].amount_usd + 1.0


# --- (e) US-domiciled buy candidate is dropped/flagged by the estate gate ---


def test_us_domiciled_buy_candidate_dropped_by_estate_gate():
    # Bonds under-target; the only candidate is a US-domiciled fund -> dropped.
    doc = _doc(
        equity_symbols=[("NVDA", None)],
        bond_symbols=[("VGSH", "US")],  # US-domiciled, non-sanctioned
    )
    verdicts = [
        _thesis("NVDA", verdict="TRIM", conviction="HIGH",
                current_weight_pct=30.0, target_weight_pct=20.0,
                current_usd_value=300_000.0),
        _thesis("VGSH", verdict="ADD", conviction="MEDIUM",
                current_weight_pct=0.0, current_usd_value=None),
    ]
    alerts = [
        _alert("equity", drift_pp=10.0, rule_fired="5pp_threshold"),
        _alert("bonds", drift_pp=-10.0, rule_fired="5pp_threshold"),
    ]

    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=[], total_book_usd=1_000_000.0,
    )
    buys = [l for l in review.legs if l.action == "BUY"]
    assert buys == []  # the only candidate was estate-gated out
    dropped_tks = {d["ticker"] for d in review.dropped_buy_candidates}
    assert "VGSH" in dropped_tks


# --- (f) every sell leg carries the taxable-event note ----------------------


def test_every_sell_leg_carries_taxable_event_note():
    doc = _doc(equity_symbols=[("NVDA", None), ("GOOG", None)])
    verdicts = [
        _thesis("NVDA", verdict="SELL", conviction="MEDIUM",
                current_weight_pct=30.0, target_weight_pct=10.0,
                current_usd_value=300_000.0),
        _thesis("GOOG", verdict="TRIM", conviction="LOW",
                current_weight_pct=15.0, target_weight_pct=8.0,
                current_usd_value=150_000.0),
    ]
    alerts = [_alert("equity", drift_pp=10.0, rule_fired="5pp_threshold")]

    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=[], total_book_usd=1_000_000.0,
    )
    sells = [l for l in review.legs if l.action in ("TRIM", "SELL")]
    assert len(sells) == 2
    for leg in sells:
        assert any("TAXABLE EVENT" in n for n in leg.notes), leg.ticker


# --- conservation / fail-loud -----------------------------------------------


def test_never_trims_more_than_position_holds():
    doc = _doc(equity_symbols=[("NVDA", None)])
    verdicts = [
        _thesis("NVDA", verdict="SELL", conviction="LOW",
                current_weight_pct=30.0, target_weight_pct=0.0,
                current_usd_value=50_000.0),  # only $50k held...
    ]
    # ...but a 30pp trim of a $1M book would be $300k. Must clamp to $50k.
    alerts = [_alert("equity", drift_pp=10.0, rule_fired="5pp_threshold")]

    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=[], total_book_usd=1_000_000.0,
    )
    sells = [l for l in review.legs if l.action in ("TRIM", "SELL")]
    assert len(sells) == 1
    assert sells[0].amount_usd <= 50_000.0


def test_missing_doc_is_cannot_review_not_silent_empty():
    review = compose_rebalance_review(
        doc=None, position_verdicts=[], alerts=[],
        thesis_flags=[], total_book_usd=1_000_000.0,
    )
    assert review.status == "cannot_review"
    assert review.cannot_review_reason == "missing_target_allocation_doc"


def test_empty_book_is_cannot_review():
    doc = _doc(equity_symbols=[("NVDA", None)])
    review = compose_rebalance_review(
        doc=doc, position_verdicts=[], alerts=[],
        thesis_flags=[], total_book_usd=0.0,
    )
    assert review.status == "cannot_review"
    assert review.cannot_review_reason == "missing_or_empty_portfolio_snapshot"


def test_news_caution_gates_a_trim():
    doc = _doc(equity_symbols=[("GOOG", None)])
    verdicts = [
        _thesis("GOOG", verdict="HOLD", conviction="HIGH",
                current_weight_pct=15.0, target_weight_pct=None,
                current_usd_value=150_000.0),
    ]
    alerts = [_alert("equity", drift_pp=6.0, rule_fired="5pp_threshold")]
    flags = [{
        "kind": "alpha_report_caution", "ticker": "GOOG",
        "severity": "warning", "dedup_key": "v1|alpha_report_caution|42.abc",
    }]
    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=flags, total_book_usd=1_000_000.0,
    )
    trims = [l for l in review.legs if l.action in ("TRIM", "SELL")]
    assert len(trims) == 1
    assert trims[0].gate_reason == "NEWS_CAUTION"


# --- (g) fallback over-trim: aggregate per-class trim capped at the overage ---
def test_fallback_legs_do_not_overtrim_the_class():
    """Two no-target positions in one over-target class must not each claim the
    full class overage (codex-flagged double-count). Aggregate trims in the
    class are capped at the class overage USD."""
    doc = _doc(equity_symbols=[("AAA", None), ("BBB", None)])
    # Both held, both gated by an intact-but-overweight TRIM verdict, NEITHER
    # has a per-position target_weight_pct -> both take the fallback sizing.
    verdicts = [
        _thesis("AAA", verdict="TRIM", conviction="MEDIUM",
                current_weight_pct=35.0, target_weight_pct=None,
                current_usd_value=350_000.0),
        _thesis("BBB", verdict="TRIM", conviction="MEDIUM",
                current_weight_pct=35.0, target_weight_pct=None,
                current_usd_value=350_000.0),
    ]
    book = 1_000_000.0
    overage_usd = 10.0 / 100.0 * book  # 10pp drift -> $100k class overage
    alerts = [_alert("equity", drift_pp=10.0, rule_fired="5pp_threshold")]

    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=[], total_book_usd=book,
    )
    trims = [l for l in review.legs if l.action in ("TRIM", "SELL")]
    total_trim = round(sum(l.amount_usd for l in trims), 2)
    # Without the per-class cap this would be ~$200k (2 x $100k). With the cap
    # it must not exceed the class overage.
    assert total_trim <= overage_usd + 0.5, (
        f"fallback legs over-trimmed: {total_trim} > overage {overage_usd}"
    )


# --- (i) the concentrated sleeve is NEVER a buy leg --------------------------


def _doc_with_concentrated(
    *, equity_symbols: list[tuple[str, str | None]],
    concentrated_symbol: str = "NVDA",
    bond_symbols: list[tuple[str, str | None]] | None = None,
) -> TargetAllocationDoc:
    """A doc whose strategic single-stock sleeve is a real concentrated_equity
    class (mirrors the live plan v67 shape) — the plan-derived exclusion source."""
    def _instr(sym: str, dom: str | None) -> AllocationInstrument:
        return AllocationInstrument(
            symbol=sym, role="primary", weight_within_class_pct=100.0, domicile=dom,
        )

    classes = [
        AllocationClassDoc(
            label="Equity core",
            snapshot_category="Core Equity",
            sigma_class="us_equity",
            target_pct=60.0,
            instruments=[_instr(s, d) for s, d in equity_symbols],
        ),
        AllocationClassDoc(
            label="Strategic single-stock (NVDA)",
            snapshot_category="NVIDIA",
            sigma_class="concentrated_equity",
            target_pct=8.0,
            instruments=[_instr(concentrated_symbol, "US")],
        ),
    ]
    if bond_symbols:
        classes.append(AllocationClassDoc(
            label="Bonds",
            snapshot_category="Defensive",
            sigma_class="bonds",
            target_pct=32.0,
            instruments=[_instr(s, d) for s, d in bond_symbols],
        ))
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18,
        nvda_cap_pct=13.0, fi_pct=4.0, provenance="test", classes=classes, glide=[],
    )


def test_concentrated_sleeve_never_a_buy_leg_even_when_equity_under_target():
    """Regression for proposal #48: TRIM IBTA -> BUY NVDA. The strategic
    single-stock sleeve (doc concentrated_equity class) must never be funded
    by a drift-fill buy leg, even with a BUY verdict in an under-target class."""
    doc = _doc_with_concentrated(
        equity_symbols=[("FUSA", "IE")],
        bond_symbols=[("IBTA", "IE")],
    )
    verdicts = [
        # bonds over-target -> IBTA trims (plan wants the weight down)
        _thesis("IBTA", verdict="TRIM", conviction="LOW",
                current_weight_pct=12.0, target_weight_pct=2.0,
                current_usd_value=120_000.0),
        # equity under-target; BOTH NVDA and FUSA carry buy-side verdicts
        _thesis("NVDA", verdict="BUY", conviction="LOW",
                current_weight_pct=55.0, current_usd_value=550_000.0),
        _thesis("FUSA", verdict="ADD", conviction="MEDIUM",
                current_weight_pct=0.3, current_usd_value=3_000.0),
    ]
    alerts = [
        _alert("bonds", drift_pp=10.0, rule_fired="5pp_threshold"),
        _alert("equity", drift_pp=-10.0, rule_fired="5pp_threshold"),
    ]
    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=[], total_book_usd=1_000_000.0,
    )
    buys = [l for l in review.legs if l.action == "BUY"]
    assert buys, "the under-target equity class should still be funded"
    assert all(l.ticker != "NVDA" for l in buys), (
        f"NVDA must never be a buy leg: {[(l.ticker, l.amount_usd) for l in buys]}"
    )
    # FUSA absorbs the funding instead of splitting with NVDA.
    assert {l.ticker for l in buys} == {"FUSA"}
    dropped = {d["ticker"]: d for d in review.dropped_buy_candidates}
    assert "NVDA" in dropped
    assert "do-not-re-concentrate" in dropped["NVDA"]["reason"]


def test_concentrated_exclusion_falls_back_to_sanctioned_constant():
    """A doc WITHOUT a concentrated_equity class still never buys NVDA —
    the exclusion falls back to the sanctioned-US-situs constant."""
    doc = _doc(
        equity_symbols=[("NVDA", None), ("FUSA", "IE")],
        bond_symbols=[("IBTA", "IE")],
    )
    verdicts = [
        _thesis("IBTA", verdict="TRIM", conviction="LOW",
                current_weight_pct=40.0, target_weight_pct=30.0,
                current_usd_value=400_000.0),
        _thesis("NVDA", verdict="BUY", conviction="LOW",
                current_weight_pct=30.0, current_usd_value=300_000.0),
        _thesis("FUSA", verdict="ADD", conviction="MEDIUM",
                current_weight_pct=1.0, current_usd_value=10_000.0),
    ]
    alerts = [
        _alert("bonds", drift_pp=10.0, rule_fired="5pp_threshold"),
        _alert("equity", drift_pp=-10.0, rule_fired="5pp_threshold"),
    ]
    review = compose_rebalance_review(
        doc=doc, position_verdicts=verdicts, alerts=alerts,
        thesis_flags=[], total_book_usd=1_000_000.0,
    )
    assert all(
        l.ticker != "NVDA" for l in review.legs if l.action == "BUY"
    )


# --- (j) stale holistic proposal is superseded on re-run ---------------------


@pytest.fixture()
def _proposal_session(tmp_path):
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'proposals.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _open_holistic_row(session, *, severity: str, summary: str = "stale"):
    from datetime import datetime, timedelta, timezone

    from argosy.state.models import ActionProposal

    now = datetime.now(timezone.utc)
    row = ActionProposal(
        user_id="ariel",
        summary=summary,
        rationale_md="stale rationale",
        suggested_payload="{}",
        severity=severity,
        surfaced_at=now,
        expires_at=now + timedelta(days=7),
        status="open",
        kind="rebalance",
        dedup_key=f"v1|rebalance|holistic_rebalance|{severity}",
        execution_state="proposed",
    )
    session.add(row)
    session.commit()
    return row


def test_stale_holistic_proposal_superseded_on_rerun(_proposal_session):
    """A fresh run supersedes the old open row even when the severity bucket
    (and therefore the dedup key) changed — the proposal-48 failure mode."""
    from datetime import datetime, timezone

    from argosy.services.holistic_rebalance_review import (
        RebalanceLeg,
        _persist_review,
        _supersede_stale_holistic_proposals,
    )
    from argosy.state.models import ActionProposal

    stale = _open_holistic_row(_proposal_session, severity="critical")
    now = datetime.now(timezone.utc)

    n = _supersede_stale_holistic_proposals(_proposal_session, "ariel", now=now)
    assert n == 1

    review = RebalanceReview(
        status="ok", summary="fresh", rationale_md="fresh",
        legs=[RebalanceLeg(
            action="TRIM", ticker="IBTA", asset_class="bonds",
            from_pct=12.0, to_pct=2.0, amount_usd=1000.0,
            gate_reason="THESIS_OVERWEIGHT",
        )],
        severity="warning",
    )
    assert _persist_review(_proposal_session, "ariel", review, now=now)

    _proposal_session.expire_all()
    stale_row = _proposal_session.get(ActionProposal, stale.id)
    assert stale_row.status == "superseded"
    open_rows = [
        r for r in _proposal_session.query(ActionProposal).all()
        if r.status == "open"
    ]
    assert len(open_rows) == 1
    assert open_rows[0].summary == "fresh"
    assert open_rows[0].dedup_key.endswith("|warning")


def test_supersede_leaves_non_holistic_rebalance_proposals_alone(_proposal_session):
    from datetime import datetime, timedelta, timezone

    from argosy.services.holistic_rebalance_review import (
        _supersede_stale_holistic_proposals,
    )
    from argosy.state.models import ActionProposal

    now = datetime.now(timezone.utc)
    other = ActionProposal(
        user_id="ariel", summary="flagsig", rationale_md="",
        suggested_payload="{}", severity="warning",
        surfaced_at=now, expires_at=now + timedelta(days=30),
        status="open", kind="rebalance",
        dedup_key="v1|rebalance|flagsig:state_observer_x:field|warning",
        execution_state="proposed",
    )
    _proposal_session.add(other)
    _proposal_session.commit()

    n = _supersede_stale_holistic_proposals(_proposal_session, "ariel", now=now)
    assert n == 0
    _proposal_session.expire_all()
    assert _proposal_session.get(ActionProposal, other.id).status == "open"


def test_zero_leg_rerun_supersedes_without_writing(_proposal_session):
    """A corrected run that composes NOTHING must still clear the stale row —
    handled by run_holistic_rebalance_review calling the supersede helper
    before deciding whether to write."""
    from datetime import datetime, timezone

    from argosy.services.holistic_rebalance_review import (
        _supersede_stale_holistic_proposals,
    )
    from argosy.state.models import ActionProposal

    stale = _open_holistic_row(_proposal_session, severity="critical")
    now = datetime.now(timezone.utc)
    assert _supersede_stale_holistic_proposals(_proposal_session, "ariel", now=now) == 1
    _proposal_session.commit()
    _proposal_session.expire_all()
    assert _proposal_session.get(ActionProposal, stale.id).status == "superseded"
    assert not [
        r for r in _proposal_session.query(ActionProposal).all()
        if r.status == "open"
    ]


# --- (h) alpha_report_caution ticker matching from caution text -------------
def test_tickers_in_text_matches_known_whole_words_only():
    from argosy.services.holistic_rebalance_review import _tickers_in_text
    known = {"NVDA", "GOOG", "AI", "MSFT"}
    text = "Caution on NVDA after the print; GOOG steady. Watch the AI theme."
    got = _tickers_in_text(text, known)
    assert got == ["AI", "GOOG", "NVDA"]  # sorted, whole-word, MSFT absent
    # whole-word: 'AID' must NOT match the 'AI' ticker
    assert _tickers_in_text("the company gave first AID", {"AI"}) == []
    # empty inputs
    assert _tickers_in_text("", known) == []
    assert _tickers_in_text("NVDA", set()) == []
