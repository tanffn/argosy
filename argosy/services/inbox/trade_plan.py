"""Trade-plan overview — ONE table over every open buy/sell decision.

The inbox used to scatter the staged sells and the buys across priority
buckets; the client asked for a single "what changes and why" surface:
current state | after changes | why, with the detail cards kept for
zoom-in. This module computes that projection. It is read-only and
derives EVERY number from raw rows: the latest portfolio snapshot
(positions + totals) and the open trade proposals. The "why" line is the
fleet's own verdict sentence from the proposal rationale — never
re-authored here (determinism states facts; the fleet authors judgment).

The cash line aggregates settled cash + T-bill-class holdings and carries
the dry-powder earmark (open ``dry_powder_discovery_reserve`` decision)
as its why — the reserve is a label on held SGOV, not a purchase.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

# T-bill-class symbols counted into the cash line. These mirror the
# instruments the dry-powder directive names as cash-equivalent
# (instantly deployable, zero drawdown); classification only — no amounts.
_CASH_EQUIVALENT_SYMBOLS = {"SGOV", "IB01"}

_MD_STRIP_RE = re.compile(r"\*\*|__|`|^#{1,6}\s+", re.MULTILINE)


def _verdict_line(rationale: str, n: int = 170) -> str:
    """The fleet's own verdict sentence, plain-text, one line."""
    text = _MD_STRIP_RE.sub("", rationale or "")
    m = re.search(
        r"Verdict:\s*(.+?)(?:(?<=[a-z0-9])\.\s|\n|$)", text, re.DOTALL | re.IGNORECASE
    )
    line = (m.group(1) if m else text).strip()
    line = " ".join(line.split())
    return line if len(line) <= n else line[: n - 1].rstrip() + "…"


def _latest_snapshot(db: Session, user_id: str):
    from argosy.state.models import PortfolioSnapshotRow

    return db.execute(
        select(PortfolioSnapshotRow)
        .where(PortfolioSnapshotRow.user_id == user_id)
        .order_by(PortfolioSnapshotRow.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def build_trade_plan(db: Session, user_id: str) -> dict[str, Any] | None:
    """The overview table, or ``None`` when no trade decision is open."""
    from argosy.state.models import ActionProposal, Proposal

    rows = (
        db.execute(
            select(Proposal)
            .where(
                Proposal.user_id == user_id,
                Proposal.status.in_(("awaiting_human", "approved")),
            )
            .order_by(Proposal.id.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    snap = _latest_snapshot(db, user_id)
    if snap is None:
        return None
    positions = json.loads(snap.positions_json or "[]")
    totals = json.loads(snap.totals_json or "{}")
    book_usd = float(totals.get("total_usd_value_k") or 0.0) * 1000.0
    cash_usd = float(totals.get("cash_balances_usd_k") or 0.0) * 1000.0

    # Aggregate held value / shares / price per symbol.
    held: dict[str, dict[str, float]] = {}
    tbill_usd = 0.0
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        usd = float(p.get("usd_value_k") or 0.0) * 1000.0
        if sym in _CASH_EQUIVALENT_SYMBOLS:
            tbill_usd += usd
        if not sym:
            continue
        agg = held.setdefault(sym, {"usd": 0.0, "shares": 0.0, "price": 0.0})
        agg["usd"] += usd
        agg["shares"] += float(p.get("shares") or 0.0)
        price = float(p.get("current_price") or 0.0)
        if price:
            agg["price"] = price

    def pct(v: float) -> float | None:
        return round(v / book_usd * 100.0, 2) if book_usd else None

    lines: list[dict[str, Any]] = []
    sells_usd = 0.0
    buys_usd = 0.0
    for r in rows:
        sym = (r.ticker or "").upper()
        h = held.get(sym) or held.get(sym.replace(".", "/")) or {
            "usd": 0.0,
            "shares": 0.0,
            "price": 0.0,
        }
        if (r.size_units or "") == "shares":
            amount = float(r.size_shares_or_currency or 0.0) * (
                h["price"] or (h["usd"] / h["shares"] if h["shares"] else 0.0)
            )
        else:
            amount = float(r.size_shares_or_currency or 0.0)
        signed = -amount if r.action == "sell" else amount
        after = max(h["usd"] + signed, 0.0)
        if r.action == "sell":
            sells_usd += amount
        else:
            buys_usd += amount
        lines.append(
            {
                "item_id": f"trade:{r.id}",
                "label": sym,
                "action": r.action,
                "current_usd": round(h["usd"]),
                "current_pct": pct(h["usd"]),
                "after_usd": round(after),
                "after_pct": pct(after),
                "delta_usd": round(signed),
                "why": _verdict_line(r.rationale_summary or ""),
            }
        )

    # Cash line: settled cash + T-bill-class holdings; the trades' net
    # proceeds land here until the deployment tranches move them. The why
    # names the SPECIFIC instruments, all read from fleet-authored records:
    # the plan's cash-sleeve instrument + target (plan_versions.current),
    # the redeploy destination (the open proceeds_redeploy row), and the
    # dry-powder earmark (its open row).
    net = sells_usd - buys_usd
    cash_current = cash_usd + tbill_usd
    why_cash = (
        "Sale proceeds settle here (cash + T-bills) until the plan's "
        "deployment tranches move them to the underfunded sleeves."
    )
    try:
        from argosy.state.models import PlanVersion

        pv = db.execute(
            select(PlanVersion).where(
                PlanVersion.user_id == user_id, PlanVersion.role == "current"
            )
        ).scalar_one_or_none()
        cash_cls = None
        if pv is not None and pv.target_allocation_json:
            ta = json.loads(pv.target_allocation_json)
            overrides = json.loads(pv.target_allocation_overrides_json or "{}")
            for cls in ta.get("classes", []):
                if "cash" in (cls.get("label") or "").lower():
                    cash_cls = cls
                    break
            if cash_cls is not None:
                label = cash_cls.get("label") or "Cash & T-bills"
                target_pct = float(
                    overrides.get(label, cash_cls.get("target_pct") or 0.0)
                )
                target_usd = book_usd * target_pct / 100.0
                instr = next(
                    (
                        i.get("symbol")
                        for i in (cash_cls.get("instruments") or [])
                        if i.get("role") == "primary" or i.get("symbol")
                    ),
                    None,
                )
                after_cash = cash_current + net
                dest = None
                rd = db.execute(
                    select(ActionProposal).where(
                        ActionProposal.user_id == user_id,
                        ActionProposal.dedup_key.like("proceeds_redeploy%"),
                        ActionProposal.status == "open",
                    )
                ).scalar_one_or_none()
                if rd is not None:
                    try:
                        dest = (
                            json.loads(rd.suggested_payload or "{}").get(
                                "destination", {}
                            )
                        ).get("symbol")
                    except (ValueError, AttributeError):
                        dest = None
                parts = []
                if instr:
                    parts.append(
                        f"Buy {instr} with settling dollars that stay parked "
                        f"(the plan's cash-sleeve instrument — not more SGOV, "
                        f"which adds US estate exposure)."
                    )
                parts.append(
                    f"Sleeve target is ${target_usd:,.0f} ({target_pct:g}%)"
                )
                excess = after_cash - target_usd
                if dest and excess > 0:
                    parts.append(
                        f"— the ~${excess:,.0f} above it buys {dest} per the "
                        f"redeploy binding."
                    )
                elif dest:
                    parts.append(
                        f"— proceeds refill this sleeve first; {dest} tranches "
                        f"ride the NVDA glide sales."
                    )
                why_cash = " ".join(parts)
    except Exception:  # noqa: BLE001 — the plain fallback why already stands
        _log.exception("trade_plan.cash_why_failed")
    dp = db.execute(
        select(ActionProposal).where(
            ActionProposal.user_id == user_id,
            ActionProposal.dedup_key == f"dry_powder_discovery_reserve:{user_id}",
            ActionProposal.status == "open",
        )
    ).scalar_one_or_none()
    if dp is not None:
        earmark = None
        try:
            payload = json.loads(dp.suggested_payload or "{}")
            earmark = (
                payload.get("reserve_usd")
                or payload.get("reserve_usd_at_current_book")
                or (payload.get("reserve") or {}).get("usd")
                or (payload.get("sizing") or {}).get("reserve_usd")
            )
        except (ValueError, TypeError, AttributeError):
            pass
        if isinstance(earmark, (int, float)) and earmark > 0:
            why_cash += (
                f" ${earmark:,.0f} of the held T-bills is proposed as the"
                " discovery dry-powder earmark (pending your confirm)."
            )
        else:
            why_cash += (
                " Part of the held T-bills is proposed as the discovery"
                " dry-powder earmark (pending your confirm)."
            )
    lines.append(
        {
            "item_id": "cash",
            "label": "Cash & T-bills",
            "action": "receives_proceeds",
            "current_usd": round(cash_current),
            "current_pct": pct(cash_current),
            "after_usd": round(cash_current + net),
            "after_pct": pct(cash_current + net),
            "delta_usd": round(net),
            "why": why_cash,
        }
    )

    return {
        "as_of": str(snap.snapshot_date or ""),
        "book_total_usd": round(book_usd),
        "lines": lines,
        "totals": {
            "sells_usd": round(sells_usd),
            "buys_usd": round(buys_usd),
            "net_to_cash_usd": round(net),
        },
    }


__all__ = ["build_trade_plan"]
