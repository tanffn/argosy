"""Integrity exclusions — user-visible, never silent drops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

from argosy.logging import get_logger

_log = get_logger("argosy.decision_integrity.exclusions")


@dataclass(frozen=True)
class IntegrityExclusion:
    """One ticker refused as actionable, with a reason the UI can show."""

    ticker: str
    reason: str
    blocked_by: str = "open_remediation"
    provenance_exemption: str | None = None

    def to_dict(self) -> dict[str, str]:
        out = {
            "ticker": self.ticker,
            "reason": self.reason,
            "blocked_by": self.blocked_by,
        }
        if self.provenance_exemption:
            out["provenance_exemption"] = self.provenance_exemption
        return out


def exclusions_for_open_remediations(
    session: Session,
    *,
    user_id: str,
    tickers: Sequence[str],
) -> list[IntegrityExclusion]:
    """Resolve open remediations into visible exclusions (sync Session)."""
    from argosy.services.decision_integrity.remediation_store import (
        list_open_remediations,
    )

    out: list[IntegrityExclusion] = []
    seen: set[str] = set()
    for t in tickers:
        if not t:
            continue
        key = str(t).strip().upper()
        if key in seen:
            continue
        seen.add(key)
        rows = list_open_remediations(session, user_id=user_id, ticker=key)
        if not rows:
            continue
        kinds = ", ".join(sorted({r.kind for r in rows}))
        reason = (
            f"open remediation on {key} ({kinds}): "
            f"{(rows[0].reason or '')[:180]}"
        )
        out.append(
            IntegrityExclusion(
                ticker=key,
                reason=reason,
                blocked_by="open_remediation",
            )
        )
    return out


async def exclusions_for_open_remediations_async(
    *,
    user_id: str,
    tickers: Sequence[str],
) -> list[IntegrityExclusion]:
    """Async path — await the query; never sessionmaker-on-async-engine."""
    from argosy.state import db as db_mod

    async with db_mod.get_session() as session:

        def _run(sync_session: Session) -> list[IntegrityExclusion]:
            return exclusions_for_open_remediations(
                sync_session, user_id=user_id, tickers=tickers,
            )

        return await session.run_sync(_run)


def exclusions_for_current_vintage(
    tickers: Sequence[str],
    *,
    fundamentals_by_ticker: Mapping[str, Mapping[str, Any]] | None = None,
    live_gather: bool = False,
) -> list[IntegrityExclusion]:
    """Evaluate *current* vintage at the decision point (not only DB rows).

    With zero remediation rows, stale equities used to remain fully
    actionable. This evaluates the vintage gate against a fundamentals
    payload. When ``live_gather`` is True and no payload is supplied, calls
    production ``_gather_fundamentals`` for the candidate set.
    """
    from argosy.services.decision_integrity.vintage_gate import (
        evaluate_vintage_gate,
    )

    payload: dict[str, Any] = {
        str(k).strip(): dict(v)
        for k, v in (fundamentals_by_ticker or {}).items()
        if isinstance(v, Mapping)
    }
    wanted = [str(t).strip() for t in tickers if t and str(t).strip()]
    if live_gather and wanted:
        missing = [t for t in wanted if t not in payload and t.upper() not in {
            k.upper() for k in payload
        }]
        if missing:
            from argosy.orchestrator.flows.plan_synthesis.inputs import (
                _gather_fundamentals,
            )

            gathered = _gather_fundamentals(missing, with_yfinance_fallback=True)
            payload.update(gathered)

    out: list[IntegrityExclusion] = []
    seen: set[str] = set()
    for t in wanted:
        key = t.upper()
        if key in seen:
            continue
        seen.add(key)
        fields = payload.get(t) or payload.get(key) or {}
        # Also try case-insensitive lookup.
        if not fields:
            for pk, pv in payload.items():
                if str(pk).upper() == key:
                    fields = pv
                    break
        vintage = evaluate_vintage_gate(t, fields if isinstance(fields, dict) else {})
        exemption = None
        if isinstance(fields, dict):
            exemption = fields.get("provenance_exemption")
        if vintage.ok:
            # Surface durable fund/cash exemption on actionable path metadata
            # even when not blocked — callers may attach to recommendations.
            if exemption:
                _log.info(
                    "integrity.provenance_exemption_recorded",
                    ticker=key,
                    exemption=exemption,
                )
            continue
        out.append(
            IntegrityExclusion(
                ticker=key,
                reason=vintage.reason,
                blocked_by=vintage.blocked_by or "vintage_stale",
                provenance_exemption=str(exemption) if exemption else None,
            )
        )
    return out


def resolve_integrity_exclusions(
    session: Session | None,
    *,
    user_id: str,
    tickers: Sequence[str],
    fundamentals_by_ticker: Mapping[str, Mapping[str, Any]] | None = None,
    enforce_current_vintage: bool | None = None,
    live_gather: bool = False,
) -> list[IntegrityExclusion]:
    """Merge open-remediation exclusions with optional current-vintage check.

    ``enforce_current_vintage`` defaults to settings.integrity_vintage_enforce
    (OFF until equity SEC liveness is real — see Stream A round report).
    """
    if enforce_current_vintage is None:
        try:
            from argosy.config import get_settings

            enforce_current_vintage = bool(
                get_settings().integrity_vintage_enforce
            )
        except Exception:  # noqa: BLE001
            enforce_current_vintage = False

    by_ticker: dict[str, IntegrityExclusion] = {}
    if session is not None:
        for e in exclusions_for_open_remediations(
            session, user_id=user_id, tickers=tickers,
        ):
            by_ticker[e.ticker] = e

    if enforce_current_vintage:
        for e in exclusions_for_current_vintage(
            tickers,
            fundamentals_by_ticker=fundamentals_by_ticker,
            live_gather=live_gather,
        ):
            # Current vintage wins when both fire — more specific.
            by_ticker[e.ticker] = e
    elif not by_ticker:
        _log.info(
            "integrity.current_vintage_enforce_off",
            ticker_count=len([t for t in tickers if t]),
        )

    return list(by_ticker.values())


def blocked_set(exclusions: Sequence[IntegrityExclusion]) -> set[str]:
    return {e.ticker.upper() for e in exclusions}


def merge_exclusion_dicts(
    exclusions: Sequence[IntegrityExclusion | dict[str, Any]],
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for e in exclusions:
        if isinstance(e, IntegrityExclusion):
            out.append(e.to_dict())
        else:
            row = {
                "ticker": str(e.get("ticker") or "").upper(),
                "reason": str(e.get("reason") or ""),
                "blocked_by": str(e.get("blocked_by") or "open_remediation"),
            }
            if e.get("provenance_exemption"):
                row["provenance_exemption"] = str(e["provenance_exemption"])
            out.append(row)
    return out


__all__ = [
    "IntegrityExclusion",
    "blocked_set",
    "exclusions_for_current_vintage",
    "exclusions_for_open_remediations",
    "exclusions_for_open_remediations_async",
    "merge_exclusion_dicts",
    "resolve_integrity_exclusions",
]
