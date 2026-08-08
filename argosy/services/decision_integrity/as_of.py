"""Provenance sidecar for fundamentals payloads (scalar contract preserved).

Stream A fix iteration 1 (blocker 8): do NOT wrap numeric values as
``{value, as_of}``. Existing callers and tests require scalars. Provenance
lives in sidecar metadata keys:

  * ``financials_as_of`` — fiscal period end the load-bearing numbers cover
  * ``most_recent_reported_period`` — latest fiscal period that has been
    *reported* (derived from earnings calendar quarter/year, not release day)
  * ``field_as_of`` — optional per-field overrides (ISO dates)
  * ``quote_as_of`` — quote day for market fields
  * ``provenance_complete`` — True only when both periods are known

Missing provenance is never defaulted to today (blocker 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# Financial-statement / ratio fields governed by the vintage gate.
LOAD_BEARING_FINANCIAL_FIELDS: frozenset[str] = frozenset(
    {
        "pe_ratio",
        "pe_ratio_ttm",
        "pe_normalized_annual",
        "forward_pe",
        "peg_ratio",
        "ev_ebitda",
        "eps_ttm",
        "eps_forward",
        "revenue_ttm",
        "revenue_growth_yoy",
        "revenue_per_share_ttm",
        "net_income_ttm",
        "earnings_growth_yoy",
        "free_cashflow",
        "dividend_yield",
        "payout_ratio",
        "gross_margin_ttm",
        "operating_margin_ttm",
        "net_margin_ttm",
        "debt_to_equity",
        "return_on_equity",
        "market_cap_m",
    }
)

MARKET_DATA_FIELDS: frozenset[str] = frozenset(
    {
        "current_price",
        "market_cap",
        "shares_outstanding",
        "52w_high",
        "52w_low",
        "beta",
    }
)

# Sidecar / metadata keys — never treated as numeric payload fields.
_META_KEYS: frozenset[str] = frozenset(
    {
        "source_url",
        "sector",
        "industry",
        "most_recent_earnings_date",  # release day (informational)
        "most_recent_reported_period",  # period-end of latest reported quarter
        "most_recent_reported_period_sourced",
        "reported_period_enrichment",
        "reported_period_enrichment_error",
        "financials_as_of",
        "quote_as_of",
        "field_as_of",
        "provenance_complete",
        "as_of",
    }
)

_QUARTER_END_DAY: dict[int, tuple[int, int]] = {
    1: (3, 31),
    2: (6, 30),
    3: (9, 30),
    4: (12, 31),
}


@dataclass(frozen=True)
class AsOfValue:
    """Legacy helper kept for tests that construct stamped pairs explicitly."""

    value: Any
    as_of: date

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "as_of": self.as_of.isoformat()}


def parse_date(raw: Any) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, (int, float)):
        try:
            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.utcfromtimestamp(ts).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            if "T" in s:
                return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


# Back-compat alias used by older call sites / tests.
_parse_date = parse_date


def unwrap_as_of(raw: Any) -> tuple[Any, date | None]:
    """Return ``(value, as_of)`` from a stamped dict, AsOfValue, or bare scalar."""
    if isinstance(raw, AsOfValue):
        return raw.value, raw.as_of
    if isinstance(raw, dict) and "value" in raw:
        return raw.get("value"), parse_date(raw.get("as_of"))
    return raw, None


def field_as_of(fields: dict[str, Any], key: str) -> date | None:
    """Resolve provenance date for one field from sidecar (never invents today)."""
    sidecar = fields.get("field_as_of")
    if isinstance(sidecar, dict) and key in sidecar:
        return parse_date(sidecar.get(key))
    if key in MARKET_DATA_FIELDS:
        return parse_date(fields.get("quote_as_of"))
    if key in LOAD_BEARING_FINANCIAL_FIELDS:
        return parse_date(fields.get("financials_as_of"))
    # Legacy wrapped shape (if any caller still passes it).
    _, as_of = unwrap_as_of(fields.get(key))
    return as_of


def format_as_of_label(raw: Any, *, as_of: date | None = None) -> str:
    """Render ``<value> (as of YYYY-MM-DD)`` when provenance is known."""
    value, embedded = unwrap_as_of(raw)
    label_date = as_of or embedded
    if value is None:
        return "null"
    if label_date is None:
        return str(value)
    return f"{value} (as of {label_date.isoformat()})"


def format_field_for_prompt(fields: dict[str, Any], key: str) -> str:
    """Prompt renderer that preserves as_of from the sidecar."""
    return format_as_of_label(fields.get(key), as_of=field_as_of(fields, key))


def period_end_for_quarter(year: int, quarter: int) -> date | None:
    q = int(quarter)
    if q not in _QUARTER_END_DAY:
        return None
    month, day = _QUARTER_END_DAY[q]
    return date(int(year), month, day)


def inferred_period_end_from_release(release: date) -> date:
    """DEPRECATED heuristic — DO NOT use for provenance decisions.

    Maps a release day onto a calendar-quarter end. Off-calendar fiscal
    issuers would get a false period and falsely PASS stale data
    (iter-2 item 4). Kept only so callers that incorrectly imported it
    still resolve; ``reported_period_from_earnings_event`` never calls it.
    """
    y, m = release.year, release.month
    if m <= 3:
        return date(y - 1, 12, 31)
    if m <= 6:
        return date(y, 3, 31)
    if m <= 9:
        return date(y, 6, 30)
    return date(y, 9, 30)


def reported_period_from_earnings_event(ev: dict[str, Any]) -> date | None:
    """Extract a *sourced* fiscal period-end from a calendar event.

    Requires explicit ``quarter`` + ``year`` from the provider. Date-only
    events return None (unknown provenance — must block), never a
    synthesized calendar-quarter end (iter-2 item 4).
    """
    q = ev.get("quarter")
    y = ev.get("year")
    try:
        if q is not None and y is not None:
            period = period_end_for_quarter(int(y), int(q))
            return period
    except (TypeError, ValueError):
        pass
    return None


def attach_provenance_sidecar(
    payload: dict[str, dict[str, Any]],
    *,
    quote_as_of: date | None = None,
) -> dict[str, dict[str, Any]]:
    """Attach provenance metadata WITHOUT wrapping numeric scalars.

    Does **not** invent ``financials_as_of`` or ``most_recent_reported_period``
    when absent — missing stays missing so the vintage gate can fail closed.
    Idempotent on already-annotated payloads.
    """
    out: dict[str, dict[str, Any]] = {}
    for ticker, fields in payload.items():
        if not isinstance(fields, dict):
            continue
        annotated = dict(fields)
        # Unwrap any legacy {value, as_of} wrappers back to scalars + sidecar.
        field_dates: dict[str, str] = {}
        existing_sidecar = annotated.get("field_as_of")
        if isinstance(existing_sidecar, dict):
            field_dates.update(
                {str(k): str(v) for k, v in existing_sidecar.items() if v}
            )
        for key, raw in list(annotated.items()):
            if key in _META_KEYS:
                continue
            if isinstance(raw, dict) and "value" in raw:
                annotated[key] = raw.get("value")
                d = parse_date(raw.get("as_of"))
                if d is not None:
                    field_dates[key] = d.isoformat()
        if field_dates:
            annotated["field_as_of"] = field_dates
        if quote_as_of is not None and not annotated.get("quote_as_of"):
            annotated["quote_as_of"] = quote_as_of.isoformat()
        data_period = parse_date(annotated.get("financials_as_of"))
        reported = parse_date(annotated.get("most_recent_reported_period"))
        # Only set when True — avoid polluting scalar payloads that tests
        # compare by exact equality (blocker 8).
        if data_period and reported:
            annotated["provenance_complete"] = True
        out[ticker] = annotated
    return out


# Deprecated name kept as alias so older imports keep working; now sidecar-only.
def stamp_fundamentals_payload(
    payload: dict[str, dict[str, Any]],
    *,
    quote_as_of: date | None = None,
    default_financials_as_of: date | None = None,
) -> dict[str, dict[str, Any]]:
    """Back-compat wrapper — ignores ``default_financials_as_of`` (blocker 1)."""
    del default_financials_as_of
    return attach_provenance_sidecar(payload, quote_as_of=quote_as_of)


def extract_earnings_date_from_yf_info(info: dict[str, Any]) -> date | None:
    """Fiscal period end from yfinance info (mostRecentQuarter preferred)."""
    for key in ("mostRecentQuarter", "lastFiscalYearEnd"):
        parsed = parse_date(info.get(key))
        if parsed is not None:
            return parsed
    return None


__all__ = [
    "AsOfValue",
    "LOAD_BEARING_FINANCIAL_FIELDS",
    "MARKET_DATA_FIELDS",
    "attach_provenance_sidecar",
    "extract_earnings_date_from_yf_info",
    "field_as_of",
    "format_as_of_label",
    "format_field_for_prompt",
    "inferred_period_end_from_release",
    "parse_date",
    "period_end_for_quarter",
    "reported_period_from_earnings_event",
    "stamp_fundamentals_payload",
    "unwrap_as_of",
]
