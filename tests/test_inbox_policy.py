"""Tests for the inbox attention-ordering policy.

Pure logic — no DB. Covers bucket assignment per kind, materiality suppression,
the expiring/blocking jump, the within-bucket sort, and the plain-English
rank_reason (which must never leak internal enums).
"""

from __future__ import annotations

from argosy.services.inbox.policy import (
    DEFAULT_POLICY,
    InboxPolicy,
    assign_bucket,
    rank_items,
    rank_reason,
)
from argosy.services.inbox.types import InboxItem, PriorityBucket


def _item(kind: str, *, signals=None, **kw) -> InboxItem:
    return InboxItem(
        id=kw.pop("id", f"{kind}:1"),
        kind=kind,  # type: ignore[arg-type]
        title=kw.pop("title", "t"),
        why_now=kw.pop("why_now", "w"),
        signals=signals or {},
        **kw,
    )


# --- bucket assignment -----------------------------------------------------


def test_overdue_plan_task_is_blocking():
    it = _item("plan_task", signals={"status": "OVERDUE", "days_overdue": 3})
    assert assign_bucket(it) == PriorityBucket.OVERDUE_BLOCKING


def test_dated_plan_task_is_commitment():
    for status in ("TODAY", "DUE_SOON", "UPCOMING"):
        it = _item("plan_task", signals={"status": status})
        assert assign_bucket(it) == PriorityBucket.PLAN_COMMITMENT


def test_sell_trade_is_risk_reduction():
    it = _item("trade", signals={"action": "sell"})
    assert assign_bucket(it) == PriorityBucket.RISK_REDUCTION


def test_buy_trade_is_opportunity():
    it = _item("trade", signals={"action": "buy"})
    assert assign_bucket(it) == PriorityBucket.OPPORTUNITY


def test_expiring_trade_jumps_to_blocking_regardless_of_action():
    # A buy that would normally be OPPORTUNITY jumps to blocking when expiring.
    it = _item("trade", signals={"action": "buy", "expiring_in_days": 2})
    assert assign_bucket(it) == PriorityBucket.OVERDUE_BLOCKING


def test_expiring_far_out_does_not_jump():
    it = _item("trade", signals={"action": "buy", "expiring_in_days": 30})
    assert assign_bucket(it) == PriorityBucket.OPPORTUNITY


def test_material_cash_surfaces():
    it = _item("cash_deploy", signals={"excess_usd": 50_000.0}, amount_usd=50_000.0)
    assert assign_bucket(it) == PriorityBucket.MATERIAL_CASH


def test_immaterial_cash_is_suppressed():
    it = _item("cash_deploy", signals={"excess_usd": 100.0}, amount_usd=100.0)
    assert assign_bucket(it) is None


def test_info_note_is_suppressed():
    it = _item("note", signals={"severity": "info"})
    assert assign_bucket(it) is None


def test_warning_note_is_observation():
    it = _item("note", signals={"severity": "warning"})
    assert assign_bucket(it) == PriorityBucket.OBSERVATION


def test_critical_risk_note_is_risk_reduction():
    it = _item("note", signals={"severity": "critical", "risk_kind": True})
    assert assign_bucket(it) == PriorityBucket.RISK_REDUCTION


def test_critical_nonrisk_note_is_observation():
    it = _item("note", signals={"severity": "critical", "risk_kind": False})
    assert assign_bucket(it) == PriorityBucket.OBSERVATION


# --- ordering --------------------------------------------------------------


def test_rank_orders_by_bucket_then_deadline():
    overdue = _item("plan_task", id="a", signals={"status": "OVERDUE", "days_overdue": 1})
    cash = _item("cash_deploy", id="b", signals={"excess_usd": 80_000.0}, amount_usd=80_000.0)
    note = _item("note", id="c", signals={"severity": "warning"})
    surfaced, suppressed = rank_items([note, cash, overdue])
    assert [i.id for i in surfaced] == ["a", "b", "c"]
    assert suppressed == []


def test_within_bucket_soonest_deadline_first():
    far = _item("plan_task", id="far", signals={"status": "DUE_SOON"}, due_at="2026-12-01")
    soon = _item("plan_task", id="soon", signals={"status": "DUE_SOON"}, due_at="2026-07-01")
    surfaced, _ = rank_items([far, soon])
    assert [i.id for i in surfaced] == ["soon", "far"]


def test_within_bucket_larger_dollars_first_when_no_deadline():
    small = _item("trade", id="small", signals={"action": "buy"}, amount_usd=1_000.0)
    big = _item("trade", id="big", signals={"action": "buy"}, amount_usd=90_000.0)
    surfaced, _ = rank_items([small, big])
    assert [i.id for i in surfaced] == ["big", "small"]


def test_suppressed_items_are_returned_not_lost():
    keep = _item("note", id="keep", signals={"severity": "critical"})
    drop = _item("note", id="drop", signals={"severity": "info"})
    surfaced, suppressed = rank_items([keep, drop])
    assert [i.id for i in surfaced] == ["keep"]
    assert [i.id for i in suppressed] == ["drop"]


# --- rank_reason -----------------------------------------------------------


def test_rank_reason_overdue_counts_days_and_money():
    it = _item(
        "plan_task",
        signals={"status": "OVERDUE", "days_overdue": 3},
        amount_usd=84_000.0,
    )
    msg = rank_reason(it, PriorityBucket.OVERDUE_BLOCKING)
    assert "Overdue by 3 days" in msg
    assert "$84k" in msg


def test_rank_reason_singular_day():
    it = _item("plan_task", signals={"status": "OVERDUE", "days_overdue": 1})
    assert "Overdue by 1 day." == rank_reason(it, PriorityBucket.OVERDUE_BLOCKING)


def test_rank_reason_material_cash():
    it = _item("cash_deploy", signals={"excess_usd": 250_000.0}, amount_usd=250_000.0)
    assert rank_reason(it, PriorityBucket.MATERIAL_CASH) == "$250k idle cash above your plan target."


def test_rank_reason_never_leaks_internal_enums():
    # The policy reads tier/status enums but must not echo them into copy.
    it = _item(
        "trade",
        signals={"action": "sell", "tier": "T2", "status": "awaiting_human"},
        amount_usd=12_000.0,
    )
    msg = rank_reason(it, PriorityBucket.RISK_REDUCTION)
    for leak in ("T2", "awaiting_human", "account_class", "tier"):
        assert leak not in msg


def test_policy_version_is_stable_and_changes_with_thresholds():
    v1 = DEFAULT_POLICY.version
    assert v1.startswith("inbox-pol-")
    assert v1 == InboxPolicy().version  # stable
    v2 = InboxPolicy(material_cash_usd=9_999.0).version
    assert v2 != v1  # a threshold change is a new version
