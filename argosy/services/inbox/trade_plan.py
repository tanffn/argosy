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
        .order_by(PortfolioSnapshotRow.imported_at.desc(), PortfolioSnapshotRow.id.desc())  # canonical head ordering (imported_at DESC, id DESC) — matches get_latest_snapshot_row; a bare id.desc() could pick a backfill/restore row over the true head (Sol BLOCK-6)
        .limit(1)
    ).scalar_one_or_none()


def build_trade_plan(
    db: Session, user_id: str, *, today: "date | None" = None
) -> dict[str, Any] | None:
    """The overview table, or ``None`` when no trade decision is open."""
    from argosy.state.models import ActionProposal, Proposal

    # Cooling proposals (user-deferred / scheduled resurfaces, e.g. a sell
    # parked for a pending evaluation) ARE part of "how will my portfolio
    # change" — they render as dated lines even though no decision card is
    # up yet. Excluding them made the table lie by omission.
    rows = (
        db.execute(
            select(Proposal)
            .where(
                Proposal.user_id == user_id,
                Proposal.status.in_(("awaiting_human", "approved", "cooling")),
            )
            .order_by(Proposal.id.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    from datetime import date  # noqa: F401  (used in the signature annotation)

    from argosy.services.current_book import load_current_book

    # Respect the caller's ``today`` (threaded from build_inbox) so the
    # staleness clock uses the actual evaluation date — NOT date.today()
    # hardcoded. This is legitimate time-simulation (the snapshot is dated
    # relative to that same today), distinct from the backdating bug where
    # today was set to the SNAPSHOT's own date to hide staleness.
    book = load_current_book(db, user_id, today=today)
    snap = book.snapshot
    if snap is None:
        return None
    # A degraded book cannot publish current money (durable unmanaged NVDA
    # unrestorable / a hard-stale unrepriceable mark / a conservation break).
    # Rather than republish an understated concentration + cash figure, degrade
    # the whole projection exactly as a missing snapshot does (Sol round-5 #3).
    if book.degraded:
        _log.warning("trade_plan.book_degraded reason=%s", book.degrade_reason)
        return None
    # SPINE GATE (Phase 3c) — money-critical projection. When enforcement is ON
    # (``spine_gate_enforce``, default OFF) degrade the projection on a NON-
    # validated book exactly as a degraded/missing snapshot does above, rather
    # than publish a concentration + cash figure from an unverified book. DEFAULT
    # (warn) config leaves this dormant — zero behavior change.
    if not book.validated:
        from argosy.config import get_settings

        if get_settings().spine_gate_enforce:
            _log.warning(
                "trade_plan.book_not_validated_enforced reason=%s",
                book.validation_reason,
            )
            return None
    # Positions come from the CONSERVED book (incl. durable unmanaged NVDA),
    # not raw positions_json which understates when Schwab NVDA is absent.
    positions = book.total
    # Denominator = the conserved book sum, NOT totals_json's total (which omits
    # the durable unmanaged NVDA and would understate every concentration %).
    book_usd = sum(float(p.get("usd_value_k") or 0.0) for p in positions) * 1000.0
    # Cash from the CONSERVED book's cash-balance rows, NOT totals_json's
    # ``cash_balances_usd_k`` — a stale/phantom totals_json cash figure inflated
    # current cash + every downstream cash_after / parking / deploy sizing
    # (Sol round-6 #2). The book is the single source of truth.
    cash_usd = sum(
        float(p.get("usd_value_k") or 0.0) * 1000.0
        for p in positions
        if "cash" in str(p.get("asset_type") or "").lower()
    )

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
    proposal_lines: list[dict[str, Any]] = []
    park_line: dict[str, Any] | None = None
    dest_line: dict[str, Any] | None = None
    dest_label: str | None = None
    dest_target_pct: float | None = None
    cash_target_pct: float | None = None
    cash_target_usd: float | None = None
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
        why = _verdict_line(r.rationale_summary or "")
        if r.status == "cooling" and r.cooling_off_until:
            why = f"From {str(r.cooling_off_until)[:10]}: {why}"
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
                "why": why,
            }
        )
        proposal_lines.append(lines[-1])

    # Deploy lines: the proceeds' destinations render as REAL rows with
    # before/after like every other line — the park instrument (IB01) and
    # the excess destination (EXUS) — followed by the cash-sleeve summary.
    # All figures derive from fleet-authored records: the plan's cash class
    # (plan_versions.current) and the open proceeds_redeploy binding.
    net = sells_usd - buys_usd
    cash_current = cash_usd + tbill_usd
    why_cash = (
        "Sale proceeds settle here (cash + T-bills) until the plan's "
        "deployment tranches move them to the underfunded sleeves."
    )
    cash_after = cash_current + net
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
        rd = db.execute(
            select(ActionProposal).where(
                ActionProposal.user_id == user_id,
                ActionProposal.dedup_key.like("proceeds_redeploy%"),
                ActionProposal.status == "open",
            )
        ).scalar_one_or_none()
        dest = None
        if rd is not None:
            try:
                dest = (
                    json.loads(rd.suggested_payload or "{}").get("destination", {})
                ).get("symbol")
            except (ValueError, AttributeError):
                dest = None
        if cash_cls is not None and dest:
            label = cash_cls.get("label") or "Cash & T-bills"
            target_pct = float(
                overrides.get(label, cash_cls.get("target_pct") or 0.0)
            )
            target_usd = book_usd * target_pct / 100.0
            park = next(
                (
                    i.get("symbol")
                    for i in (cash_cls.get("instruments") or [])
                    if i.get("symbol")
                ),
                None,
            )
            raw_after = cash_current + net
            excess = max(raw_after - target_usd, 0.0)
            park_buy = max(min(net - excess, target_usd - cash_current), 0.0)
            cash_target_pct, cash_target_usd = target_pct, target_usd
            for cls in ta.get("classes", []):
                if any(
                    (i.get("symbol") or "").upper() == dest.upper()
                    for i in (cls.get("instruments") or [])
                ):
                    dest_label = cls.get("label")
                    dest_target_pct = float(
                        overrides.get(dest_label, cls.get("target_pct") or 0.0)
                    )
                    break
            if park and park_buy > 0:
                park_held = held.get(park, {"usd": 0.0})["usd"]
                lines.append(
                    {
                        "item_id": f"deploy:{park}",
                        "label": park,
                        "action": "buy",
                        "current_usd": round(park_held),
                        "current_pct": pct(park_held),
                        "after_usd": round(park_held + park_buy),
                        "after_pct": pct(park_held + park_buy),
                        "delta_usd": round(park_buy),
                        "why": (
                            "Parked proceeds buy the plan's cash-sleeve "
                            "instrument (Irish UCITS 0-1yr Treasuries) — not "
                            "more SGOV, which adds US estate exposure."
                        ),
                    }
                )
                park_line = lines[-1]
            if excess > 0:
                dest_held = held.get(dest, {"usd": 0.0})["usd"]
                lines.append(
                    {
                        "item_id": f"deploy:{dest}",
                        "label": dest,
                        "action": "buy",
                        "current_usd": round(dest_held),
                        "current_pct": pct(dest_held),
                        "after_usd": round(dest_held + excess),
                        "after_pct": pct(dest_held + excess),
                        "delta_usd": round(excess),
                        "why": (
                            "Proceeds above the cash-sleeve target buy the "
                            "plan's biggest-gap sleeve per the redeploy "
                            "binding; the larger tranches ride the NVDA "
                            "glide sales."
                        ),
                    }
                )
                dest_line = lines[-1]
            cash_after = cash_current + net - excess
            why_cash = (
                f"Sleeve lands at its {target_pct:g}% plan target "
                f"(${target_usd:,.0f}) — working cash + ILS expense tranche "
                f"+ held SGOV + the {park or 'T-bill'} purchase above."
            )
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
            "label": "Cash & T-bills sleeve",
            "action": "receives_proceeds",
            "current_usd": round(cash_current),
            "current_pct": pct(cash_current),
            "after_usd": round(cash_after),
            "after_pct": pct(cash_after),
            "delta_usd": round(cash_after - cash_current),
            "why": why_cash,
        }
    )
    cash_line = lines[-1]

    # Sleeve grouping — the client reads the table sleeve by sleeve:
    #   + sleeve header (current -> after vs its plan target)
    #     -- movement lines
    # Legacy singles hold every trade proposal (their plan target is 0);
    # the park buy nests under the cash sleeve; the excess buy under its
    # own destination sleeve.
    groups: list[dict[str, Any]] = []
    if proposal_lines:
        groups.append(
            {
                "label": "Legacy single stocks",
                "target_pct": 0.0,
                "target_usd": 0,
                "current_usd": sum(l["current_usd"] for l in proposal_lines),
                "after_usd": sum(l["after_usd"] for l in proposal_lines),
                "why": "Off-plan single names — plan v74 target is 0%; "
                "staged redeploys, not bare exits.",
                "lines": proposal_lines,
            }
        )
    groups.append(
        {
            "label": cash_line["label"],
            "target_pct": cash_target_pct,
            "target_usd": round(cash_target_usd) if cash_target_usd else None,
            "current_usd": cash_line["current_usd"],
            "after_usd": cash_line["after_usd"],
            "why": why_cash,
            "lines": [park_line] if park_line else [],
        }
    )
    if dest_line is not None:
        dest_target_usd = (
            round(book_usd * dest_target_pct / 100.0) if dest_target_pct else None
        )
        groups.append(
            {
                "label": dest_label or dest_line["label"],
                "target_pct": dest_target_pct,
                "target_usd": dest_target_usd,
                "current_usd": dest_line["current_usd"],
                "after_usd": dest_line["after_usd"],
                "why": None,
                "lines": [dest_line],
            }
        )

    return {
        "as_of": str(snap.snapshot_date or ""),
        "book_total_usd": round(book_usd),
        "lines": lines,
        "groups": groups,
        "totals": {
            "sells_usd": round(sells_usd),
            "buys_usd": round(buys_usd),
            "net_to_cash_usd": round(net),
        },
    }


__all__ = ["build_trade_plan"]
