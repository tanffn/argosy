"""Integrity exclusions — user-visible, never silent drops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class IntegrityExclusion:
    """One ticker refused as actionable, with a reason the UI can show."""

    ticker: str
    reason: str
    blocked_by: str = "open_remediation"

    def to_dict(self) -> dict[str, str]:
        return {
            "ticker": self.ticker,
            "reason": self.reason,
            "blocked_by": self.blocked_by,
        }


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
            out.append(
                {
                    "ticker": str(e.get("ticker") or "").upper(),
                    "reason": str(e.get("reason") or ""),
                    "blocked_by": str(e.get("blocked_by") or "open_remediation"),
                }
            )
    return out


__all__ = [
    "IntegrityExclusion",
    "blocked_set",
    "exclusions_for_open_remediations",
    "exclusions_for_open_remediations_async",
    "merge_exclusion_dicts",
]
