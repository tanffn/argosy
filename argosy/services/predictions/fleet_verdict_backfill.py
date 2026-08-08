"""Idempotent backfill: fleet verdicts → predictions (no survivorship bias).

Reads ALL ``verdicts`` rows (settled and superseded). Rejected / expired
proposals are still recorded as the call the fleet made — dropping them
would inflate the baseline. Entry prices are resolved **as of the
verdict timestamp**, never as a single undated last price.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.services.predictions.writers import (
    FLEET_VERDICT_SOURCE,
    write_fleet_verdict_prediction,
)
from argosy.state.models import Prediction, Proposal, Verdict

_log = get_logger("argosy.services.predictions.fleet_verdict_backfill")

EntryPriceAsOfResolver = Callable[[str, datetime], float | None]


@dataclass
class FleetBackfillSummary:
    scanned: int = 0
    written: int = 0
    versioned: int = 0
    skipped_no_entry: int = 0
    recorded_rejected_proposal: int = 0
    recorded_unsettled: int = 0
    skipped_kind: int = 0
    tickers_written: list[str] = field(default_factory=list)
    pending_entry_tickers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "written": self.written,
            "versioned": self.versioned,
            "skipped_no_entry": self.skipped_no_entry,
            "recorded_rejected_proposal": self.recorded_rejected_proposal,
            "recorded_unsettled": self.recorded_unsettled,
            "skipped_kind": self.skipped_kind,
            "tickers_written": list(self.tickers_written),
            "pending_entry_tickers": list(self.pending_entry_tickers),
            "note": (
                "skipped_no_entry still writes durable pending-entry rows; "
                "count is the subset lacking as-of prices"
            ),
        }


def _proposal_status_for_run(
    session: Session,
    *,
    user_id: str,
    subject: str,
    decision_run_id: int | None,
) -> str | None:
    if decision_run_id is None:
        return None
    rows = session.execute(
        select(Proposal).where(
            Proposal.user_id == user_id,
            Proposal.decision_run_id == decision_run_id,
            Proposal.ticker == subject,
        )
    ).scalars().all()
    if not rows:
        return None
    # Prefer a terminal status if any.
    statuses = [(p.status or "").lower() for p in rows]
    for terminal in ("rejected", "cancelled", "expired", "blocked", "approved"):
        if terminal in statuses:
            return terminal
    return statuses[0] if statuses else None


def backfill_fleet_verdict_predictions(
    session: Session,
    user_id: str,
    *,
    entry_prices: dict[str, float] | None = None,
    price_resolver: EntryPriceAsOfResolver | None = None,
    only_subjects: set[str] | None = None,
) -> FleetBackfillSummary:
    """Create versioned predictions for every fleet verdict (incl. superseded).

    ``entry_prices`` maps SUBJECT → price (fixture override).
    ``price_resolver(subject, as_of)`` supplies event-time prices.
    When neither yields a price, a durable pending-entry row is still
    written so the verdict cannot vanish from ``total_predictions``.
    """
    summary = FleetBackfillSummary()
    entry_prices = {k.upper(): float(v) for k, v in (entry_prices or {}).items()}
    only = {s.upper() for s in only_subjects} if only_subjects else None

    verdicts = list(
        session.execute(
            select(Verdict)
            .where(Verdict.user_id == user_id)
            .order_by(Verdict.id.asc())
        ).scalars()
    )

    for row in verdicts:
        summary.scanned += 1
        subject = (row.subject or "").upper()
        if only is not None and subject not in only:
            continue
        action = (row.verdict or "").strip().upper()
        if action not in {
            "BUY", "ADD", "TRIM", "SELL", "HOLD", "WAIT",
        }:
            summary.skipped_kind += 1
            continue

        if not row.settled:
            summary.recorded_unsettled += 1

        prop_status = _proposal_status_for_run(
            session,
            user_id=user_id,
            subject=subject,
            decision_run_id=row.source_decision_run_id,
        )
        if prop_status in {"rejected", "cancelled", "expired", "blocked"}:
            summary.recorded_rejected_proposal += 1

        as_of = row.created_at
        px = entry_prices.get(subject)
        if px is None and price_resolver is not None:
            try:
                resolved = price_resolver(subject, as_of)
                px = float(resolved) if resolved is not None else None
            except Exception:  # noqa: BLE001
                px = None
        if px is None:
            summary.skipped_no_entry += 1
            summary.pending_entry_tickers.append(subject)

        triggers: list[dict[str, Any]] = []
        if row.revisit_triggers_json:
            try:
                parsed = json.loads(row.revisit_triggers_json)
                if isinstance(parsed, list):
                    triggers = [t for t in parsed if isinstance(t, dict)]
            except (TypeError, ValueError):
                triggers = []

        before_ids = {
            int(i)
            for i in session.execute(
                select(Prediction.id).where(
                    Prediction.user_id == user_id,
                    Prediction.source == FLEET_VERDICT_SOURCE,
                    Prediction.ticker == subject,
                )
            ).scalars()
        }

        reasons = []
        if prop_status:
            reasons.append(f"proposal_status={prop_status}")
        if not row.settled:
            reasons.append("verdict_unsettled_at_backfill")

        pred = write_fleet_verdict_prediction(
            session,
            user_id,
            verdict_id=row.id,
            ticker=subject,
            verdict=action,
            event_at=as_of,
            entry_price=px,
            revisit_triggers=triggers,
            next_validation=row.next_validation,
            conviction=row.conviction,
            decision_run_id=row.source_decision_run_id,
            missing_field_reasons=reasons or None,
        )
        if pred is None:
            continue
        if pred.id not in before_ids and pred.entry_price is not None:
            summary.written += 1
            summary.tickers_written.append(subject)
        elif pred.id not in before_ids:
            summary.written += 1  # pending-entry durable row
        else:
            # May be a new version of an existing lineage.
            after_ids = {
                int(i)
                for i in session.execute(
                    select(Prediction.id).where(
                        Prediction.user_id == user_id,
                        Prediction.source == FLEET_VERDICT_SOURCE,
                        Prediction.ticker == subject,
                    )
                ).scalars()
            }
            if after_ids - before_ids:
                summary.versioned += 1
                summary.tickers_written.append(subject)

    _log.info("fleet_backfill.done", **summary.to_dict())
    return summary
