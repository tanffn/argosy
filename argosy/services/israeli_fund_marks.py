"""Shared position-level fund repricing for snapshot refresh and current books."""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

from argosy.logging import get_logger

_log = get_logger(__name__)


def is_israeli_fund(pos: dict[str, Any]) -> bool:
    return pos.get("quote_market") == "TASE_FUND" and bool(
        re.fullmatch(r"5[0-9]{6}", str(pos.get("security_id") or ""))
    )


def attach_fund_identity(pos: dict[str, Any], *, security_id: str,
                         broker_price: float | None, broker_avg_price: float | None) -> dict:
    """Preserve raw broker prices separately from reporting-currency values.

    The export reports USD value but local native unit prices. Until the
    denomination is verified by the quote provider, use the USD value/quantity
    for the reporting mark; never multiply units by raw agorot as dollars.
    """
    if not re.fullmatch(r"5[0-9]{6}", security_id):
        return dict(pos)
    out = dict(pos, security_id=security_id, quote_market="TASE_FUND",
               broker_price=broker_price, broker_avg_price=broker_avg_price,
               price_source="broker_reporting_value")
    shares = float(pos.get("shares") or 0)
    value = pos.get("current_value_local")
    out["current_price"] = float(value) / shares if shares > 0 and value is not None else None
    out["avg_price"] = None  # raw quote-currency cost retained above, not USD
    return out


def reprice_fund_position(pos: dict[str, Any], *, fx_usd_nis: float | None,
                          today: date, quote_fn=None) -> dict | None:
    from argosy.adapters.data.israeli_funds import MAX_NAV_AGE_DAYS, get_fund_quote
    if not is_israeli_fund(pos):
        return None
    try:
        shares = float(pos.get("shares") or 0)
        fx = float(fx_usd_nis or 0)
        if not all(math.isfinite(x) and x > 0 for x in (shares, fx)):
            return None
        q = (quote_fn or get_fund_quote)(pos["security_id"], today=today)
        if q.security_id != pos["security_id"] or not 0 <= (today - q.as_of).days <= MAX_NAV_AGE_DAYS:
            return None
        if str(pos.get("price_source") or "").startswith("https://") and pos.get("valued_as_of"):
            if q.as_of < date.fromisoformat(str(pos["valued_as_of"])[:10]):
                return None  # Do not replace a newer verified NAV with an older one.
        price = q.price_ils
        if not math.isfinite(price) or price <= 0:
            return None
        # Compare like units. Raw broker NAV is in agorot once the provider has
        # confirmed the fund denomination; subsequent marks are canonical ILS.
        old = (pos.get("current_price") if pos.get("currency") in ("ILS", "NIS")
               and pos.get("price_source") != "broker_reporting_value" else None)
        if old is None and pos.get("broker_price") is not None:
            old = float(pos["broker_price"]) / 100
        if old and not 0.5 <= price / float(old) <= 2.0:
            return None
        out = dict(pos)
        from argosy.services.holding_books import mark_is_stale
        out.update(currency="ILS", current_price=price, current_value_local=shares * price,
                   usd_value_k=shares * price / fx / 1000, valued_as_of=q.as_of,
                   mark_stale=mark_is_stale(q.as_of, today=today), repriced=True, price_source=q.source_url,
                   price_fetched_at=q.fetched_at.isoformat(), price_evidence_sha256=q.sha256)
        if pos.get("broker_avg_price") is not None:
            out["avg_price"] = float(pos["broker_avg_price"]) / 100
        return out
    except Exception as exc:
        _log.warning("israeli_fund.quote_unavailable", security_id=pos.get("security_id"), error=str(exc))
        return None


def enrich_from_broker(positions, broker_positions, *, source: str) -> list:
    """Attach exact broker identity without inferring a fund from an index name.

    Supports the two existing Leumi parsers. Name and quantity must both
    match uniquely. Existing canonical marks are left unchanged on replay.
    """
    from argosy.ingest.tsv import PortfolioPosition
    result = []
    for p in positions:
        if not (p.location or "").lower().startswith("leumi") or is_israeli_fund(p.model_dump()):
            result.append(p)
            continue
        matches = [h for h in broker_positions
                   if re.fullmatch(r"5[0-9]{6}", str(h.security_id))
                   and " ".join((getattr(h, "name", None) or getattr(h, "name_he", "")).split())
                   == " ".join((p.details or "").split())]
        if len(matches) != 1:
            result.append(p)
            continue
        h = matches[0]
        if p.shares is None or not math.isclose(p.shares, h.quantity, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f"Broker identity quantity mismatch for {h.security_id}")
        data = attach_fund_identity(p.model_dump(), security_id=h.security_id,
                                   broker_price=h.last_price,
                                   broker_avg_price=getattr(h, "avg_cost", getattr(h, "avg_buy_price", None)))
        data["identity_source"] = source
        result.append(PortfolioPosition(**data))
    return result


def fund_tsv_cells(cells: list[str], broker, *, observed_as_of: date | None) -> list[str]:
    """Extend the legacy 13-column format without losing fund identity/units."""
    from argosy.ingest.tsv import _parse_position_row
    from argosy.services.portfolio_ingest.xls_osh_pair import _position_to_cells
    if not re.fullmatch(r"5[0-9]{6}", str(broker.security_id)):
        return cells
    p = _parse_position_row(cells, line_no=0)
    if p is None:
        raise ValueError("Cannot serialize broker fund position")
    p.shares = broker.quantity  # never the rounded legacy cell
    p.observed_as_of = observed_as_of
    p.valued_as_of = observed_as_of
    enriched = enrich_from_broker([p], [broker], source="leumi-xls")
    return _position_to_cells(enriched[0])
