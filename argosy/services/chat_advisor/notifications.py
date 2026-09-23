"""Quiet notification projection from the canonical Inbox."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from argosy.services.chat_advisor.contracts import Citation, OutboxEvent, Principal
from argosy.services.chat_advisor.retrieval import RetrievalService
from argosy.services.inbox.service import build_inbox
from argosy.services.inbox.types import InboxItem, PriorityBucket

_TERMINAL_STATUSES = {"expired", "superseded", "resolved", "cancelled", "rejected", "completed"}
_OBSERVATION_FIELDS = frozenset({
    "generated_at", "observed_at", "updated_at", "fetched_at", "refreshed_at", "as_of",
    "verified_at", "price_verified_at", "price_as_of", "quote_timestamp",
    "market_price", "current_price", "last_price", "quote_price",
})


def _semantic_body(value: Any) -> Any:
    """Exclude observation churn, not dated obligations or proposed amounts."""
    if isinstance(value, dict):
        return {key: (_canonical_values(child) if key == "blockers" else _semantic_body(child))
                for key, child in value.items() if key not in _OBSERVATION_FIELDS}
    if isinstance(value, (list, tuple)):
        return [_semantic_body(child) for child in value]
    return value


def _canonical_values(raw: Any) -> list[str]:
    values = [raw] if isinstance(raw, str) else (raw or [])
    return sorted({str(value).strip() for value in values if str(value).strip()})


class NotificationSourceUnavailable(RuntimeError):
    """Canonical Inbox was partial, so alert state must not advance or close."""


def _canonical_blockers(item: InboxItem) -> list[str]:
    raw = item.body.get("blockers") or item.signals.get("blockers") or []
    if isinstance(raw, str):
        raw = [raw]
    return sorted({str(value).strip() for value in raw if str(value).strip()})


def _material_fields(item: InboxItem) -> tuple[Any, ...]:
    status = str(item.body.get("status") or item.signals.get("status") or "actionable").lower()
    amount = item.body.get("amount") if "amount" in item.body else item.amount_usd
    currency = item.body.get("currency") or ("USD" if amount is not None else None)
    rationale_version = (
        item.body.get("rationale_version") or item.signals.get("rationale_version") or "unversioned"
    )
    return (
        item.id,
        item.kind,
        item.title,
        _semantic_body(item.body),
        status,
        amount,
        currency,
        item.expires_at,
        item.due_at,
        rationale_version,
        _canonical_blockers(item),
        str(item.signals.get("severity", "")).lower(),
    )


def material_version(item: InboxItem) -> str:
    encoded = json.dumps(
        _material_fields(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _eligible(item: InboxItem, now: datetime) -> bool:
    status = str(item.body.get("status") or item.signals.get("status") or "actionable").lower()
    if status in _TERMINAL_STATUSES or item.signals.get("expired"):
        return False
    if item.primary_action is None or item.bucket == PriorityBucket.OBSERVATION:
        return False
    if item.expires_at:
        try:
            expiry = datetime.fromisoformat(item.expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry <= now.astimezone(UTC):
                return False
        except ValueError:
            return False  # malformed freshness never becomes an urgent page
    return True


def _event(item: InboxItem, generated_at: str) -> OutboxEvent:
    version = material_version(item)
    refs = [Citation(ref.source, ref.ref_id, generated_at, version=version,
                     label=item.title, category=item.kind) for ref in item.source_refs]
    # Project the existing upstream severity; overdue paperwork is not an emergency.
    critical = str(item.signals.get("severity", "")).lower() == "critical"
    body = " ".join(item.title.split())
    if len(body) > 120:
        body = body[:117].rsplit(" ", 1)[0] + "…"
    if critical:
        reason = " ".join(item.why_now.split())
        body = f"{body} — {reason[:180]}"
    return OutboxEvent(
        semantic_key=f"inbox:{item.id}:{item.kind}",
        material_version=version,
        category="alert.critical" if critical else f"inbox.{item.kind}",
        body=body,
        citations=refs,
        bypasses_quiet_hours=critical,
        action_id=item.id,
        expires_at=(
            datetime.fromisoformat(item.expires_at.replace("Z", "+00:00"))
            if item.expires_at
            else None
        ),
    )


class NotificationProducer:
    """Produces candidates only; transport owns dedup, quiet hours and delivery."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        *,
        retrieval: RetrievalService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if session_factory is None and retrieval is None:
            raise ValueError(
                "NotificationProducer needs a read-only session_factory or RetrievalService"
            )
        self._session_factory = session_factory
        self._retrieval = retrieval
        self._clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def _session(self, principal: Principal) -> Iterator[Session]:
        if self._retrieval is not None:
            with self._retrieval.session(principal) as db:
                yield db
            return
        assert self._session_factory is not None
        db = self._session_factory()
        try:
            if db.bind is not None and db.bind.dialect.name == "sqlite":
                db.connection().exec_driver_sql("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()

    async def daily_overview(self, principal: Principal, *, now: datetime):
        from .daily_overview import build_daily_overview

        retrieval = self._retrieval or RetrievalService(session_factory=self._session_factory)
        return await build_daily_overview(retrieval, principal, now=now)

    def current_events(self, principal: Principal) -> list[OutboxEvent]:
        now = self._clock()
        with self._session(principal) as db:
            feed = build_inbox(db, user_id=principal.household_user_id, now=now)
        # dropped is an audit of deliberate policy suppression/deduplication,
        # not a completeness flag. Use the canonical source diagnostics instead.
        issues = list(feed.issues)
        if not issues:
            issues = [row for row in feed.dropped if row.get("reason") == "source_error"]
        if issues:
            reasons = sorted({str(row.get("code") or row.get("reason") or "unknown") for row in issues})
            raise NotificationSourceUnavailable(
                "Canonical Inbox source could not be fully read "
                f"({', '.join(reasons)}); notification state was not evaluated."
            )
        return [_event(item, feed.generated_at) for item in feed.items if _eligible(item, now)]

    def still_current(self, principal: Principal, event: OutboxEvent) -> bool:
        return any(
            candidate.semantic_key == event.semantic_key
            and candidate.material_version == event.material_version
            for candidate in self.current_events(principal)
        )


__all__ = ["NotificationProducer", "NotificationSourceUnavailable", "material_version"]
