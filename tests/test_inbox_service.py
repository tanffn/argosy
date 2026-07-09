"""Tests for build_inbox — the assembly over today's sources.

Uses a focused file-backed SQLite session seeded with Proposal + ActionProposal
rows. The plan-task and cash adapters return empty without a plan/snapshot
(verified here as graceful), so this test targets the trade + note adapters,
shadow exclusion, dedupe, materiality suppression, and the liveness metadata.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.inbox.service import build_inbox
from argosy.services.inbox.types import PriorityBucket
from argosy.state.models import ActionProposal, Base, Proposal, User

_NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
_TODAY = _NOW.date()


@pytest.fixture
def db(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'inbox.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def _trade(s, **kw):
    defaults = dict(
        user_id="ariel",
        ticker="AAA",
        action="buy",
        size_shares_or_currency=10,
        size_units="shares",
        instrument="stock",
        order_type="market",
        tier="T2",
        account_class="main",
        status="awaiting_human",
        rationale_summary="Because the thesis holds.",
        shadow=0,
    )
    defaults.update(kw)
    row = Proposal(**defaults)
    s.add(row)
    s.commit()
    return row


def _note(s, **kw):
    defaults = dict(
        user_id="ariel",
        summary="A thing to look at",
        rationale_md="Some detail.",
        suggested_payload="{}",
        severity="warning",
        kind="note_only",
        status="open",
        surfaced_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
    )
    defaults.update(kw)
    row = ActionProposal(**defaults)
    s.add(row)
    s.commit()
    return row


def test_empty_inbox_is_quiet(db):
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.quiet is True
    assert feed.items == []
    assert feed.liveness.pending_decisions == 0
    assert feed.liveness.cash_within_band is True
    assert feed.liveness.no_overdue_tasks is True
    assert feed.policy_version.startswith("inbox-pol-")


def test_shadow_proposal_never_surfaces(db):
    _trade(db, ticker="SHADOW", shadow=1, action="sell")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.quiet is True


def test_non_actionable_status_excluded(db):
    _trade(db, ticker="DRAFT", status="draft")
    _trade(db, ticker="DONE", status="executed_paper")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.quiet is True


def test_sell_is_risk_reduction_buy_is_opportunity_ordered(db):
    _trade(db, ticker="BUYME", action="buy")
    _trade(db, ticker="SELLME", action="sell")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    kinds = [(i.title, i.bucket) for i in feed.items]
    # Sell (risk reduction, bucket 2) ranks above buy (opportunity, bucket 5).
    assert kinds[0][1] == PriorityBucket.RISK_REDUCTION
    assert "SELLME" in kinds[0][0]
    assert kinds[1][1] == PriorityBucket.OPPORTUNITY
    assert "BUYME" in kinds[1][0]


def test_expiring_buy_jumps_to_top(db):
    _trade(db, ticker="SLOW", action="sell")  # risk reduction, bucket 2
    _trade(
        db,
        ticker="EXPIRING",
        action="buy",
        expires_at=_NOW + timedelta(days=1),
    )
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.items[0].bucket == PriorityBucket.OVERDUE_BLOCKING
    assert "EXPIRING" in feed.items[0].title
    assert "Expires in 1 day" in feed.items[0].rank_reason


def test_approved_proposal_offers_execute(db):
    _trade(db, ticker="APP", status="approved", action="buy")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.items[0].primary_action.intent == "execute"
    assert feed.items[0].primary_action.requires_confirmation is True


def test_info_note_suppressed_warning_surfaces(db):
    _note(db, summary="info note", severity="info")
    _note(db, summary="warning note", severity="warning")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    titles = [i.title for i in feed.items]
    assert "warning note" in titles
    assert "info note" not in titles
    # The suppressed one is recorded for the debug view, not lost.
    assert any(d["reason"] == "below_materiality" for d in feed.dropped)


def test_decision_kind_info_proposal_surfaces_as_decision_row(db):
    """The live-incident class: an OPEN decision-kind proposal (its acceptance
    changes plan/execution state) must surface whatever its severity. The
    glide-schedule verdict (kind=update_plan_assumption, severity=info) was
    audit-only and Ariel could not find where to accept it."""
    _note(
        db,
        summary="NVDA deconcentration pace adjudicated — confirm the schedule",
        severity="info",
        kind="update_plan_assumption",
        dedup_key="plan_glide_schedule_verdict:ariel:nvda",
    )
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    rows = [i for i in feed.items if "adjudicated" in i.title]
    assert rows, "a decision-kind proposal can never be audit-only"
    it = rows[0]
    assert it.bucket is not None
    assert it.primary_action.intent == "accept"
    assert "decision" in it.rank_reason.lower()


def test_note_only_info_stays_audit_only(db):
    _note(db, summary="observer chatter", severity="info", kind="note_only")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert not [i for i in feed.items if "observer chatter" in i.title]
    assert any(d["reason"] == "below_materiality" for d in feed.dropped)


def test_flagsig_decision_kind_chatter_stays_severity_gated(db):
    """Auto-derived flag-signature proposals are observer commentary, not a
    directive — the decision-kind bypass must not promote them (mirrors the
    home greeting's flagsig exclusion)."""
    _note(
        db,
        summary="flagsig rebalance chatter",
        severity="info",
        kind="rebalance",
        dedup_key="flagsig:abc123",
    )
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert not [i for i in feed.items if "flagsig rebalance chatter" in i.title]


def test_one_decision_one_row_per_dedup_key(db):
    """One decision = one row: a single OPEN row per dedup_key (the writer
    updates in place) yields exactly one inbox item; a superseded sibling of
    the same dedup_key never adds a second row."""
    _note(
        db,
        summary="old verdict",
        severity="info",
        kind="update_plan_assumption",
        dedup_key="plan_glide_schedule_verdict:ariel:nvda",
        status="superseded",
    )
    _note(
        db,
        summary="current verdict — confirm",
        severity="info",
        kind="update_plan_assumption",
        dedup_key="plan_glide_schedule_verdict:ariel:nvda",
    )
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    verdict_rows = [
        i for i in feed.items
        if any(r.source == "action_proposal" for r in i.source_refs)
        and "verdict" in i.title
    ]
    assert len(verdict_rows) == 1
    assert verdict_rows[0].title == "current verdict — confirm"


def test_critical_risk_note_is_risk_reduction(db):
    _note(db, summary="concentration risk", severity="critical", kind="rebalance")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.items[0].bucket == PriorityBucket.RISK_REDUCTION


def test_no_internal_enums_in_client_projection(db):
    _trade(db, ticker="AAA", action="sell", tier="T3", status="awaiting_human")
    _note(db, summary="x", severity="critical", kind="concentration")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    blob = str(feed.to_dict())
    for leak in ("awaiting_human", "account_class", '"tier"', "T3", "shadow"):
        assert leak not in blob


def test_cash_directive_uses_canonical_engine_buy_list(db, monkeypatch):
    """The proactive cash directive surfaces the CANONICAL engine's buy list
    (core + discovery sleeve), not the detector's sleeve-less proposals."""
    import argosy.services.inbox.service as svc
    import argosy.services.unallocated_cash_detector as det

    class _Ev:
        excess_usd = 30_000.0
        headline = "Cash sits above your plan target."
        snapshot_date = "2026-06-30"
        proposals = []  # the OLD engine would supply nothing here

    canonical_rows = [
        {"instrument": "MOON", "asset_class": "High-growth potential",
         "amount_usd": 1_500.0, "tier": "high", "rationale": "discovery BUY"},
        {"instrument": "CSPX", "asset_class": "US broad-market core",
         "amount_usd": 28_500.0, "tier": "core", "rationale": "gap-fill"},
    ]
    monkeypatch.setattr(det, "detect_unallocated_cash_overage", lambda *a, **k: _Ev())
    monkeypatch.setattr(svc, "_cash_buy_list", lambda *a, **k: canonical_rows)

    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    cash = [i for i in feed.items if i.kind == "cash_deploy"]
    assert cash, "cash directive should surface when cash is over target"
    assert cash[0].body["buy_list"] == canonical_rows


def test_cash_directive_falls_back_to_detector_when_canonical_unavailable(db, monkeypatch):
    """If the canonical engine can't build (no plan / error), the item still
    renders from the detector's proposals — the inbox never blanks."""
    import argosy.services.inbox.service as svc
    import argosy.services.unallocated_cash_detector as det

    class _P:
        instrument = "AAA"
        asset_class = "equity"
        amount_usd = 100.0
        rationale = "gap"

    class _Ev:
        excess_usd = 5_000.0
        headline = "h"
        snapshot_date = None
        proposals = [_P()]

    monkeypatch.setattr(det, "detect_unallocated_cash_overage", lambda *a, **k: _Ev())
    monkeypatch.setattr(svc, "_cash_buy_list", lambda *a, **k: None)

    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    cash = [i for i in feed.items if i.kind == "cash_deploy"][0]
    assert cash.body["buy_list"] == [
        {"instrument": "AAA", "asset_class": "equity", "amount_usd": 100.0, "rationale": "gap"}
    ]


def _policy_sell(status):
    from argosy.services.nvda_policy_sell import NvdaPolicySell

    if status == "sell_due":
        return NvdaPolicySell(
            status="sell_due", category="policy", tranche_nis=500_000.0,
            nvda_current_pct=57.0, nvda_cap_pct=13.0, n_quarters=8,
            headline="Trim NVDA ~₪500,000 this quarter — 57% over your 13% cap.",
            tax_note="Realizes Israeli CGT; paced over the glide.",
        )
    return NvdaPolicySell(
        status="no_action", category="policy", tranche_nis=0.0,
        nvda_current_pct=0.0, nvda_cap_pct=0.0, n_quarters=0,
        headline="NVDA within its cap.", tax_note="",
    )


def test_policy_sell_surfaces_when_due(db, monkeypatch):
    import argosy.services.nvda_policy_sell as nps

    monkeypatch.setattr(nps, "assess_nvda_policy_sell", lambda **k: _policy_sell("sell_due"))
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    sell = [i for i in feed.items if i.id == "nvda_policy_sell"]
    assert sell, "the glide policy sell should surface proactively"
    assert "NVDA" in sell[0].why_now


def test_policy_sell_quiet_when_no_action(db, monkeypatch):
    import argosy.services.nvda_policy_sell as nps

    monkeypatch.setattr(nps, "assess_nvda_policy_sell", lambda **k: _policy_sell("no_action"))
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert not [i for i in feed.items if i.id == "nvda_policy_sell"]


def test_policy_sell_deduped_when_proposal_already_open(db, monkeypatch):
    """When the monthly cycle already routed an approval-pending decon tranche,
    _adapt_trades surfaces it — the read-only policy-sell item must not double up."""
    import argosy.services.nvda_policy_sell as nps
    from argosy.services.breach_router import DECON_TRANCHE_MARKER

    monkeypatch.setattr(nps, "assess_nvda_policy_sell", lambda **k: _policy_sell("sell_due"))
    _trade(
        db, ticker="NVDA", action="sell", status="awaiting_human",
        rationale_summary=f"[{DECON_TRANCHE_MARKER}] NVDA 57% > 13% cap tranche.",
    )
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert not [i for i in feed.items if i.id == "nvda_policy_sell"]
    # The trade proposal itself still surfaces (via the trade adapter).
    assert [i for i in feed.items if i.kind == "trade" and "NVDA" in i.title]


def test_funnel_shadow_proposal_surfaces_beta_view_first(db):
    """A calibrating (shadow) funnel proposal is EXPOSED beta (nothing hidden) but
    view-first — no blind approve/execute until it's promoted out of beta."""
    _trade(db, ticker="NVDA", action="sell", status="awaiting_human",
           source="decision_funnel", shadow=1)
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    nvda = [i for i in feed.items if i.kind == "trade" and "NVDA" in i.title]
    assert nvda, "funnel beta proposal should be exposed, not hidden"
    it = nvda[0]
    assert it.signals.get("beta") is True
    assert it.primary_action.intent == "view_reasoning"


def test_non_funnel_shadow_proposal_stays_hidden(db):
    """Shadow is still a valid hide for non-funnel proposals — the beta exposure is
    scoped to the decision funnel, not a blanket un-hide."""
    _trade(db, ticker="ZZZ", action="buy", status="awaiting_human", shadow=1)
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert not [i for i in feed.items if "ZZZ" in i.title]


def test_trade_plan_overview_built_from_raw_rows(db):
    """The overview table derives current|after|why from the snapshot +
    open proposals, ends with the cash line, and rides the feed dict."""
    import json as _json

    from argosy.state.models import PortfolioSnapshotRow

    db.add(
        PortfolioSnapshotRow(
            user_id="ariel",
            snapshot_date=_TODAY,
            imported_at=_NOW,
            positions_json=_json.dumps(
                [
                    {"symbol": "AAA", "shares": 100.0, "current_price": 50.0,
                     "usd_value_k": 5.0, "asset_type": "Stock"},
                    {"symbol": "SGOV", "shares": 10.0, "current_price": 100.0,
                     "usd_value_k": 1.0, "asset_type": "Defensive"},
                ]
            ),
            totals_json=_json.dumps(
                {"total_usd_value_k": 10.0, "cash_balances_usd_k": 2.0}
            ),
        )
    )
    db.commit()
    _trade(
        db, ticker="AAA", action="sell", size_shares_or_currency=40,
        size_units="shares",
        rationale_summary="**Verdict:** exit the stale half. More prose after.",
    )
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    tp = feed.trade_plan
    assert tp is not None
    line = next(l for l in tp["lines"] if l["label"] == "AAA")
    assert line["current_usd"] == 5000
    assert line["delta_usd"] == -2000  # 40 sh × $50
    assert line["after_usd"] == 3000
    assert line["why"].startswith("exit the stale half")
    cash = tp["lines"][-1]
    assert cash["item_id"] == "cash"
    assert cash["current_usd"] == 3000  # $2k cash + $1k SGOV
    assert cash["after_usd"] == 5000
    assert tp["totals"]["net_to_cash_usd"] == 2000
    assert feed.to_dict()["trade_plan"] is tp


def test_trade_plan_absent_without_open_trades(db):
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.trade_plan is None
    assert feed.to_dict()["trade_plan"] is None


def test_debug_dict_exposes_signals_and_dropped(db):
    _note(db, summary="info note", severity="info")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    d = feed.to_dict(debug=True)
    assert d["dropped"]  # populated in debug
    # client projection hides it
    assert feed.to_dict(debug=False)["dropped"] == []
