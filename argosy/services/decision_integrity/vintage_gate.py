"""Vintage gate — period-vs-period provenance check (Stream A).

Rule (blocker 3): compare the fiscal period the data *covers*
(``financials_as_of``) to the most recent fiscal period that has been
*reported* (``most_recent_reported_period``). Stale means the payload
covers an earlier period than the latest reported one.

  * Q1 data (2026-03-31) when Q2 has been reported (2026-06-30) → BLOCK
  * Q2 data (2026-06-30) after the Q2 release → PASS

Unknown provenance (blocker 1): missing data period OR missing reported
period is a BLOCK — never a silent pass. Never invents dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from argosy.services.decision_integrity.as_of import (
    LOAD_BEARING_FINANCIAL_FIELDS,
    field_as_of,
    parse_date,
)


@dataclass(frozen=True)
class StaleField:
    field: str
    as_of: date
    reported_period: date
    value: Any = None


@dataclass(frozen=True)
class VintageGateResult:
    ok: bool
    ticker: str
    data_period: date | None = None
    most_recent_reported_period: date | None = None
    stale_fields: tuple[StaleField, ...] = ()
    reason: str = ""
    blocked_by: str | None = None

    @property
    def block(self) -> bool:
        return not self.ok

    # Back-compat alias used by earlier tests / call sites.
    @property
    def most_recent_earnings_date(self) -> date | None:
        return self.most_recent_reported_period


def evaluate_vintage_gate(
    ticker: str,
    fields: dict[str, Any] | None,
    *,
    most_recent_reported_period: date | None = None,
    most_recent_earnings_date: date | None = None,  # legacy kw; ignored if period set
    require_load_bearing: bool = True,
) -> VintageGateResult:
    """Fail closed on unknown or stale financial provenance."""
    t = (ticker or "").strip().upper()
    payload = fields if isinstance(fields, dict) else {}

    data_period = parse_date(payload.get("financials_as_of"))
    reported = most_recent_reported_period or parse_date(
        payload.get("most_recent_reported_period")
    )
    # Legacy: if only a release date was supplied, do NOT compare period to
    # release day (that was the inverted bug). Require an explicit period.
    del most_recent_earnings_date

    if data_period is None:
        return VintageGateResult(
            ok=False,
            ticker=t,
            data_period=None,
            most_recent_reported_period=reported,
            reason=(
                f"provenance_unknown:{t}: missing financials_as_of "
                "(cannot determine which fiscal period the numbers cover)"
            ),
            blocked_by="provenance_unknown",
        )

    if reported is None:
        return VintageGateResult(
            ok=False,
            ticker=t,
            data_period=data_period,
            most_recent_reported_period=None,
            reason=(
                f"provenance_unknown:{t}: missing most_recent_reported_period "
                "(cannot determine whether a later quarter has been reported)"
            ),
            blocked_by="provenance_unknown",
        )

    # Presence of load-bearing numbers without per-field as_of falls back to
    # financials_as_of (sidecar). If a field_as_of entry is older than the
    # reported period, flag that field specifically.
    stale: list[StaleField] = []
    has_load_bearing = False
    for key in LOAD_BEARING_FINANCIAL_FIELDS:
        if key not in payload or payload.get(key) is None:
            continue
        has_load_bearing = True
        as_of = field_as_of(payload, key) or data_period
        if as_of < reported:
            stale.append(
                StaleField(
                    field=key,
                    as_of=as_of,
                    reported_period=reported,
                    value=payload.get(key),
                )
            )

    if require_load_bearing and not has_load_bearing:
        # Empty fundamentals — not a vintage claim; caller may still block
        # via remediation. Treat as ok for vintage specifically.
        return VintageGateResult(
            ok=True,
            ticker=t,
            data_period=data_period,
            most_recent_reported_period=reported,
            reason="ok_no_load_bearing_fields",
        )

    # Whole-payload period lag (even when individual field_as_of absent).
    if data_period < reported and not stale:
        stale.append(
            StaleField(
                field="financials_as_of",
                as_of=data_period,
                reported_period=reported,
                value=data_period.isoformat(),
            )
        )

    if stale or data_period < reported:
        # Ensure we block when data_period lags even if field loop filled nothing.
        if data_period < reported and not stale:
            pass  # unreachable due to above, kept for clarity
        names = ", ".join(sorted({s.field for s in stale})) or "financials_as_of"
        return VintageGateResult(
            ok=False,
            ticker=t,
            data_period=data_period,
            most_recent_reported_period=reported,
            stale_fields=tuple(stale),
            reason=(
                f"vintage_stale:{t}: data period {data_period.isoformat()} "
                f"predates most recent reported period {reported.isoformat()} "
                f"(fields: {names})"
            ),
            blocked_by="vintage_stale",
        )

    return VintageGateResult(
        ok=True,
        ticker=t,
        data_period=data_period,
        most_recent_reported_period=reported,
        reason="ok",
    )


def evaluate_vintage_gate_for_payload(
    payload: dict[str, dict[str, Any]],
    *,
    ticker: str | None = None,
) -> list[VintageGateResult]:
    if ticker:
        t = ticker.upper()
        return [evaluate_vintage_gate(t, payload.get(t) or payload.get(ticker))]
    return [
        evaluate_vintage_gate(sym, fields)
        for sym, fields in payload.items()
        if isinstance(fields, dict)
    ]


__all__ = [
    "StaleField",
    "VintageGateResult",
    "evaluate_vintage_gate",
    "evaluate_vintage_gate_for_payload",
]
