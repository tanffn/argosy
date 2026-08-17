"""Canonical net-worth bases — single-sourced so every surface reads ONE
labeled number.

The total-incl-residence basis (investable holdings + real-estate NET EQUITY)
is computed here as a pure helper. Both the Wealth Dashboard
(``wealth_dashboard._net_worth``) and the plan numeric resolver call it, so the
dashboard headline (₪14.05M) and the resolver figure are the SAME number by
construction — they cannot diverge.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

    from argosy.state.models import PortfolioSnapshotRow

log = logging.getLogger(__name__)


def real_estate_equity_for_snapshot(
    *,
    snapshot: "PortfolioSnapshotRow | None",
    fx_usd_nis: float | None = None,
    session: "Session | None" = None,
    user_id: str | None = None,
):
    """Per-property real-estate NET equity for a snapshot — the ONE computation
    the dashboard/resolver total-net-worth basis, the plan export's residence
    breakdown, and the codex raw-data packet all bind to (owner-estimate
    property values from the ingested owner sheet's ``real_estate_json``, with
    payment-ledger / impairment overrides).

    Returns a ``RealEstateEquity`` (see ``real_estate_equity``) or ``None``
    when the snapshot carries no real-estate rows.
    """
    if snapshot is None:
        return None

    from argosy.services.real_estate_equity import compute_real_estate_equity

    try:
        re_rows = json.loads(snapshot.real_estate_json or "[]")
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    re_objs = [SimpleNamespace(**r) for r in re_rows if isinstance(r, dict)]
    if not re_objs:
        return None
    loan_override: dict[str, float] = {}
    value_override: dict[str, float] = {}
    if session is not None and user_id is not None:
        from argosy.services.real_estate_ledger import (
            load_property_ledgers,
            load_real_estate_overrides,
        )
        price_by_prop = {
            getattr(o, "location", None): getattr(o, "value_local", None)
            for o in re_objs
            if (getattr(o, "role", "") or "").strip().lower() == "home"
            and getattr(o, "location", None)
            and getattr(o, "value_local", None) is not None
        }
        ledgers = load_property_ledgers(
            session, user_id=user_id, total_price_by_property=price_by_prop
        )
        loan_override = {
            k: lg.remaining_local for k, lg in ledgers.items()
            if lg.remaining_local is not None
        }
        # Impairment / write-off overrides (e.g. a bust property worth 0
        # whose mortgage was never drawn) — apply to BOTH value and loan
        # so headline net worth matches the panel (no phantom equity).
        overrides = load_real_estate_overrides(session, user_id=user_id)
        value_override = {
            k: o.current_value_local for k, o in overrides.items()
            if o.current_value_local is not None
        }
        for k, o in overrides.items():
            if o.loan_local is not None:
                loan_override[k] = o.loan_local
    return compute_real_estate_equity(
        re_objs,
        fx_usd_nis=getattr(snapshot, "fx_usd_nis", None) or fx_usd_nis,
        fx_usd_eur=getattr(snapshot, "fx_usd_eur", None),
        loan_override=loan_override, value_override=value_override,
    )


def real_estate_stub_usd_k(snapshot: "PortfolioSnapshotRow | None") -> float:
    """USD-k value of the legacy real-estate STUB rows inside the position
    block (e.g. the "$69K Aborad" row) — the amount the total-incl-residence
    basis swaps OUT before adding the full per-property net equity."""
    if snapshot is None:
        return 0.0
    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except (json.JSONDecodeError, TypeError, ValueError):
        positions = []
    return sum(
        float(p.get("usd_value_k") or 0.0)
        for p in positions
        if isinstance(p, dict) and (p.get("asset_type") or "").lower() == "real estate"
    )


def total_net_worth_incl_residence(
    *,
    snapshot: "PortfolioSnapshotRow | None",
    fx_usd_nis: float,
    session: "Session | None" = None,
    user_id: str | None = None,
) -> tuple[float | None, float | None]:
    """True net worth = investable holdings + real-estate NET EQUITY.

    The snapshot's ``total_usd_value_k`` carries only the legacy "$69K Aborad"
    real-estate stub. Real net worth replaces that with the full per-property
    net equity (Home − Loan, FX-converted) — the same figure the Real-estate
    panel shows — so net worth and the panel agree.

    When ``session``/``user_id`` are supplied, the canonical payment ledger
    (``real_estate_ledger``) drives the per-property remaining, EXACTLY as the
    Real-estate panel does — otherwise headline net worth would stay understated
    by the paid-down amount while the panel shows the new equity (the cross-
    surface inconsistency this whole change exists to kill).

    PROVENANCE (output-trust doctrine): the property values behind the equity
    component are OWNER ESTIMATES from the ingested owner sheet
    (``real_estate_json``) — a stored, traceable source, but NOT auditable from
    broker raw holdings. Surfaces rendering this basis must label the residence
    component as an owner estimate (unaudited); see ``plan_export`` and the
    codex raw-holdings packet (``raw_holdings_block``).
    """
    if snapshot is None:
        return None, None

    # Prefer the TOTAL book (snapshot + durable unmanaged) when a session is
    # available — totals_json alone understates NW after an incomplete TSV that
    # omitted Schwab/NVDA. Fail soft to None when the book is degraded so we
    # never publish a confidently understated headline.
    base_k: float | None = None
    if session is not None and user_id is not None:
        try:
            from argosy.services.holding_books import (
                investable_usd_k,
                load_total_book,
                parse_positions_json,
            )

            snap_date = getattr(snapshot, "snapshot_date", None)
            raw = parse_positions_json(snapshot.positions_json)
            book = load_total_book(
                session, user_id, raw,
                snapshot_date=snap_date,
                # Live valuation clock — shared with dashboard / snapshot.
            )
            if book.degraded:
                log.warning(
                    "net_worth_bases.degraded user=%s reason=%s",
                    user_id, book.degrade_reason,
                )
                return None, None
            book_k = investable_usd_k(book.total)
            # Output-trust: publish the auditable row sum, never an inflated
            # totals_json that invents money absent from the positions.
            try:
                totals = json.loads(snapshot.totals_json or "{}")
                totals_k = totals.get("total_usd_value_k")
                totals_k_f = float(totals_k) if totals_k is not None else None
            except (TypeError, ValueError, json.JSONDecodeError):
                totals_k_f = None
            if (
                totals_k_f is not None
                and totals_k_f > book_k + max(1.0, 0.02 * max(book_k, totals_k_f))
            ):
                log.warning(
                    "net_worth_bases.totals_exceed_rows user=%s "
                    "book_k=%.1f totals_k=%.1f — refusing inflated total",
                    user_id, book_k, totals_k_f,
                )
                return None, None
            base_k = book_k
        except Exception as exc:  # noqa: BLE001
            log.warning("net_worth_bases.total_book_failed err=%s", exc)
            base_k = None

    if base_k is None:
        try:
            totals = json.loads(snapshot.totals_json or "{}")
        except json.JSONDecodeError:
            totals = {}
        total_usd_k = totals.get("total_usd_value_k")
        if total_usd_k is None:
            return None, None
        base_k = float(total_usd_k)

    # Swap the legacy real-estate stub (the "$69K Aborad" row in the position
    # block) for the full per-property net equity — so net worth includes real
    # estate properly and matches the Real-estate panel.
    re_stub_k = real_estate_stub_usd_k(snapshot)
    re_net_k = 0.0
    try:
        eq = real_estate_equity_for_snapshot(
            snapshot=snapshot, fx_usd_nis=fx_usd_nis,
            session=session, user_id=user_id,
        )
        if eq is not None:
            re_net_k = eq.total_net_usd_k
    except (TypeError, ValueError):
        pass

    usd = (base_k - re_stub_k + re_net_k) * 1000.0
    if usd <= 0:
        return None, None
    return usd * fx_usd_nis, usd


__all__ = [
    "real_estate_equity_for_snapshot",
    "real_estate_stub_usd_k",
    "total_net_worth_incl_residence",
]
