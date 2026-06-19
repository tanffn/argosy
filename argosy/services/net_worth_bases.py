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
    """
    if snapshot is None:
        return None, None

    from argosy.services.real_estate_equity import compute_real_estate_equity

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
    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except json.JSONDecodeError:
        positions = []
    re_stub_k = sum(
        float(p.get("usd_value_k") or 0.0)
        for p in positions
        if isinstance(p, dict) and (p.get("asset_type") or "").lower() == "real estate"
    )
    re_net_k = 0.0
    try:
        re_rows = json.loads(snapshot.real_estate_json or "[]")
        re_objs = [SimpleNamespace(**r) for r in re_rows if isinstance(r, dict)]
        if re_objs:
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
            eq = compute_real_estate_equity(
                re_objs,
                fx_usd_nis=getattr(snapshot, "fx_usd_nis", None) or fx_usd_nis,
                fx_usd_eur=getattr(snapshot, "fx_usd_eur", None),
                loan_override=loan_override, value_override=value_override,
            )
            re_net_k = eq.total_net_usd_k
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    usd = (base_k - re_stub_k + re_net_k) * 1000.0
    if usd <= 0:
        return None, None
    return usd * fx_usd_nis, usd


__all__ = ["total_net_worth_incl_residence"]
