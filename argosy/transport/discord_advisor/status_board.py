"""Deterministic Discord board for the canonical scheduled-job registry."""

from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from argosy.services.chat_advisor.contracts import Citation, OutboxEvent
from argosy.services.chat_advisor.notification_policy import OPERATIONAL
from argosy.services.chat_advisor.outbound import OutboundFilter
from argosy.state import db as db_mod
from argosy.state.models import JobRun

_MATERIAL_VERSION = "status-board-v1"
_PREFIX = "status-board:"
_PAGE_BUDGET = 1900


class StatusBoardRefreshError(RuntimeError):
    """Refresh failed after the visible prior board was marked stale."""


def _now() -> datetime:
    return datetime.now(UTC)


def _local(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _job_block(view: Any, *, enabled: bool, tz: ZoneInfo, last_success: datetime | None) -> str:
    metadata = view.metadata
    if not enabled:
        state = "DISABLED"
    else:
        health = str(view.health or "unknown").upper()
        status = str(view.last_run_status or "unknown")
        state = f"{health} · last run: {status}"
    lines = [
        f"**{metadata.name}** — {state}",
        f"Schedule: {metadata.schedule_human or 'unscheduled'}",
        f"Last run: {_local(view.last_run_at, tz)}",
        f"Last success: {_local(last_success, tz)}",
        f"Next run: {'disabled' if not enabled else _local(view.next_run_at, tz)}",
    ]
    if view.currently_running_run_id is not None:
        lines.append(f"Running receipt: {view.currently_running_run_id}")
    if view.last_run_error:
        lines.append(f"Details: {view.last_run_error}")
    return OutboundFilter.redact("\n".join(lines))[:1000]


async def _last_successes(registry: Any, store: Any, names: list[str]) -> dict[str, datetime]:
    """Read canonical successful JobRun receipts; never infer through health."""
    provider = getattr(registry, "last_successes", None)
    if provider is not None:  # narrow test/alternate registry seam
        value = provider(names)
        return await value if inspect.isawaitable(value) else value
    async with db_mod.get_session(user_id=store.household_user_id) as session:
        rows = (
            await session.execute(
                select(
                    JobRun.job_name,
                    func.max(func.coalesce(JobRun.finished_at, JobRun.started_at)),
                )
                .where(JobRun.job_name.in_(names), JobRun.status == "ok")
                .group_by(JobRun.job_name)
            )
        ).all()
    return {str(name): completed_at for name, completed_at in rows if completed_at is not None}


async def critical_job_events(registry: Any, store: Any) -> list[OutboxEvent]:
    """Background job notices belong to status, not an investment chat reply."""
    views = await current_job_views(registry, store)
    red = sorted((view for view in views
                  if str(view.health).lower() == "red"
                  and bool(getattr(registry.get_job(view.metadata.name), "enabled", True))),
                 key=lambda view: view.metadata.name)
    if not red:
        return []
    names = [view.metadata.name for view in red]
    successes = await _last_successes(registry, store, names)
    # Successful recovery starts a new episode; repeated failures stay quiet.
    version = hashlib.sha256(repr([(name, successes.get(name)) for name in names]).encode()).hexdigest()
    label = ", ".join(name.replace("_", " ") for name in names[:3])
    if len(names) > 3:
        label += f", +{len(names) - 3} more"
    return [OutboxEvent(
        semantic_key="system:job-health", material_version=version, category=OPERATIONAL,
        body=f"Background tasks: {len(names)} need attention — {label}. See the task details on this board. This is separate from chat research.",
        citations=[Citation("job", name, _now().isoformat()) for name in names],
        bypasses_quiet_hours=False,
    )]


async def current_job_views(registry: Any, store: Any) -> list[Any]:
    """Retain historic receipts, but use current evidence for knowledge follow-ups."""
    from dataclasses import replace

    views = await registry.list()
    if store is None or not any(view.metadata.name == "annual" for view in views):
        return views
    from argosy.services.decision_readiness import collect_decision_readiness

    async with db_mod.get_session(user_id=store.household_user_id) as session:
        readiness = await collect_decision_readiness(session, user_id=store.household_user_id)
    annual = next((job for job in readiness["jobs"] if job["name"] == "annual"), {})
    if annual.get("status") not in {"knowledge_recovered", "knowledge_incomplete"}:
        return views
    knowledge = readiness.get("knowledge") or {}
    urgent = bool(readiness.get("urgent_findings"))
    return [replace(view,
                    health="red" if urgent else "green" if annual["status"] == "knowledge_recovered" else "amber",
                    last_run_error=(f"Current evidence: {knowledge.get('verified', 0)}/{knowledge.get('total', 0)} documents verified; "
                                    f"{knowledge.get('outstanding', 0)} follow-ups. Historical run details remain in Job history."))
            if view.metadata.name == "annual" else view for view in views]


def _pages(blocks: list[str], *, refreshed: datetime, tz: ZoneInfo) -> list[str]:
    stamp = _local(refreshed, tz)
    preamble = (
        "**Argosy automated tasks**\n"
        f"Last refreshed: {stamp}. This heartbeat is not proof the PC stayed online afterward."
    )
    pages: list[list[str]] = [[]]
    for block in blocks:
        candidate = "\n\n".join([preamble, *pages[-1], block])
        if len(candidate) <= _PAGE_BUDGET or not pages[-1]:
            pages[-1].append(block)
        else:
            pages.append([block])
    count = len(pages)
    rendered = []
    for index, page in enumerate(pages, start=1):
        heading = preamble + (f"\nPage {index}/{count}" if count > 1 else "")
        text = "\n\n".join([heading, *page])
        rendered.extend(OutboundFilter.chunk(text))
    return rendered


def _semantic_key(channel_id: str, page: int) -> str:
    return f"{_PREFIX}{channel_id}:{page}"


async def _existing_rows(store: Any, channel_id: str) -> list[Any]:
    prefix = f"{_PREFIX}{channel_id}:"
    if hasattr(store, "status_board_pages"):
        rows = await store.status_board_pages(prefix)
        return sorted(rows, key=lambda row: int(row.semantic_key.rsplit(":", 1)[1]))
    rows = [
        row
        for row in await store.active_sent_outbox()
        if row.semantic_key.startswith(prefix) and row.material_version == _MATERIAL_VERSION
    ]
    return sorted(rows, key=lambda row: int(row.semantic_key.rsplit(":", 1)[1]))


async def _upsert_page(*, store: Any, gateway: Any, channel_id: str, page: int, body: str) -> None:
    key = _semantic_key(channel_id, page)
    nonce = hashlib.sha256(f"{key}:{_MATERIAL_VERSION}".encode()).hexdigest()[:24]
    event = OutboxEvent(
        semantic_key=key,
        material_version=_MATERIAL_VERSION,
        category="status_board",
        body=body,
    )
    if hasattr(store, "reserve_status_board_page"):
        row = await store.reserve_status_board_page(event, nonce=nonce)
        if row.status == "board_sent" and row.sent_message_id:
            await gateway.edit(channel_id, row.sent_message_id, body)
            await store.update_status_board_body(row.id, body=body)
            return
        message_id = await gateway.send(channel_id, body, nonce=row.nonce)
        await store.mark_status_board_sent(row.id, message_id=message_id, body=body)
        return
    existing = await store.latest_sent_outbox(key)
    if existing is not None and existing.sent_message_id:
        await gateway.edit(channel_id, existing.sent_message_id, body)
        return
    row = await store.enqueue_outbox(event)
    if row.status == "sent" and row.sent_message_id:
        await gateway.edit(channel_id, row.sent_message_id, body)
        return
    message_id = await gateway.send(channel_id, body, nonce=nonce)
    await store.mark_outbox_sent(row.id, message_id)


async def _surface_refresh_failure(
    *, store: Any, gateway: Any, channel_id: str, tz: ZoneInfo, error: Exception
) -> None:
    rows = await _existing_rows(store, channel_id)
    banner = OutboundFilter.redact(
        "**STATUS BOARD REFRESH FAILED**\n"
        f"Attempted: {_local(_now(), tz)}\n"
        f"Reason: {type(error).__name__}: {error}\n"
        "The prior board below is retained and may be stale."
    )
    if rows:
        prior = rows[0].body or "Prior board content unavailable; do not infer healthy state."
        body = OutboundFilter.chunk(f"{banner}\n\n{prior}")[0]
        if rows[0].sent_message_id:
            await gateway.edit(channel_id, rows[0].sent_message_id, body)
            if hasattr(store, "update_status_board_body"):
                await store.update_status_board_body(rows[0].id, body=body)
            return
        await _upsert_page(store=store, gateway=gateway, channel_id=channel_id, page=1, body=body)
        return
    await _upsert_page(store=store, gateway=gateway, channel_id=channel_id, page=1, body=banner)


async def publish_status_board(
    *, registry: Any, gateway: Any, store: Any, channel_id: str, timezone: str
) -> None:
    """Refresh stable board messages from ``JobRegistry.list``.

    The registry is the only job-state source. No model, job trigger, or
    administrative capability is involved.
    """
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown status-board timezone: {timezone}") from exc
    try:
        views = await current_job_views(registry, store)
        names = [view.metadata.name for view in views]
        successes = await _last_successes(registry, store, names)
        blocks = []
        for view in sorted(views, key=lambda item: item.metadata.name):
            job = registry.get_job(view.metadata.name)
            enabled = bool(getattr(job, "enabled", True))
            blocks.append(
                _job_block(
                    view,
                    enabled=enabled,
                    tz=tz,
                    last_success=successes.get(view.metadata.name),
                )
            )
        if not blocks:
            blocks = ["No automated tasks are registered. This is unknown/empty, not all healthy."]
        pages = _pages(blocks, refreshed=_now(), tz=tz)
    except Exception as exc:  # registry failure must never erase the prior board
        await _surface_refresh_failure(
            store=store, gateway=gateway, channel_id=channel_id, tz=tz, error=exc
        )
        raise StatusBoardRefreshError(
            "status-board refresh failed; prior board marked stale"
        ) from exc

    existing = await _existing_rows(store, channel_id)
    for index, body in enumerate(pages, start=1):
        await _upsert_page(
            store=store, gateway=gateway, channel_id=channel_id, page=index, body=body
        )
    for row in existing[len(pages) :]:
        retired = OutboundFilter.redact(
            "**Argosy automated tasks**\n"
            f"This former page is no longer needed. Last refreshed: {_local(_now(), tz)}."
        )
        if row.sent_message_id:
            await gateway.edit(channel_id, row.sent_message_id, retired)
            if hasattr(store, "update_status_board_body"):
                await store.update_status_board_body(row.id, body=retired)


__all__ = ["StatusBoardRefreshError", "publish_status_board"]
