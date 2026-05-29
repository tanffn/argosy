"""Sprint A commit #6 — :class:`DiscordListenerJob` tests.

Spec: ``docs/superpowers/specs/2026-05-29-jobs-registry-design.md`` §6.

Coverage:

* HELLO → ``on_connected`` callback fires → ``connection_status`` flips
  from ``"reconnecting"`` to ``"connected"``. Then the listener returns
  cleanly → ``exit_intent="clean"``, status returns to ``"stopped"``.
* ``exit_intent`` is ``"clean"`` on natural return.
* ``exit_intent`` is coerced to ``"crashed"`` by the supervisor when
  ``run()`` raises (the job itself does not stamp the intent on the
  raising path — the supervisor handles that per the
  :class:`LongRunningJob.exit_intent` contract).
* Missing creds (``creds=None``) → ``run()`` fast-clean-exits with
  ``exit_intent="clean"``, ``connection_status`` stays ``"stopped"``,
  the supervisor records a single ``status='ok'`` row and does NOT
  restart.

NO real Discord websocket; the listener body is injected via the
``listener_fn`` constructor parameter so the gateway protocol is never
touched here. The existing
:mod:`tests.test_discord_listener` module covers
:func:`run_discord_listener` end-to-end against fake events.

Test command::

    D:/Projects/financial-advisor/.venv/Scripts/python.exe -m pytest \\
        -m "not llm_eval" tests/test_discord_listener_job.py -v
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from argosy.services.discord_listener import DiscordCreds
from argosy.services.jobs import JobRegistry
from argosy.services.jobs.discord_listener_job import (
    DiscordListenerJob,
    discord_listener_metadata,
)
from argosy.state import db as db_mod
from argosy.state.models import JobRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FAKE_TOKEN = "MTk1NDg2NDU0OTQ4OTQ5MTI0.GExample.token_value_padding_xyz"


def _make_creds() -> DiscordCreds:
    return DiscordCreds(
        bot_token=_FAKE_TOKEN,
        channel_id=1234567890,
        server_id=9876543210,
    )


def _make_session_factory() -> Any:
    """Return a MagicMock sufficient as a sessionmaker substitute.

    We never call .add() / .commit() / .close() in these tests — the
    listener body is stubbed — but the constructor stores the factory
    so make it callable.
    """
    return MagicMock(name="session_factory")


# ---------------------------------------------------------------------------
# Unit-level — constructor + status transitions via injected listener_fn
# ---------------------------------------------------------------------------


def test_constructor_initial_state() -> None:
    """Fresh job: ``connection_status='stopped'``, ``exit_intent='unset'``."""
    job = DiscordListenerJob(_make_creds(), _make_session_factory())
    assert job.connection_status() == "stopped"
    assert job.exit_intent == "unset"
    assert job.name == "discord_listener"


def test_metadata_shape() -> None:
    """``discord_listener_metadata`` matches the spec §6 mapping (ingest /
    long_running=True / cron=None)."""
    meta = discord_listener_metadata()
    assert meta.name == "discord_listener"
    assert meta.source_kind == "ingest"
    assert meta.long_running is True
    assert meta.schedule_cron is None


@pytest.mark.asyncio
async def test_run_hello_callback_flips_status_to_connected() -> None:
    """Simulated listener: HELLO arrives → on_connected fires → status
    flips to 'connected'. Then listener returns → exit_intent='clean',
    status returns to 'stopped'.
    """
    creds = _make_creds()
    sf = _make_session_factory()

    observed_status_sequence: list[str] = []

    async def fake_listener(session_factory, *, creds, on_connected=None, **_):
        # Status is 'reconnecting' BEFORE we fire the callback (the job
        # set it before awaiting us).
        observed_status_sequence.append(job.connection_status())
        # Simulate HELLO ack → fire the status hook.
        assert on_connected is not None
        on_connected()
        observed_status_sequence.append(job.connection_status())
        # Simulate a graceful close — return normally.
        return None

    job = DiscordListenerJob(creds, sf, listener_fn=fake_listener)
    await job.run()

    assert observed_status_sequence == ["reconnecting", "connected"]
    # After return, finally block restored status to 'stopped'.
    assert job.connection_status() == "stopped"
    assert job.exit_intent == "clean"


@pytest.mark.asyncio
async def test_run_natural_return_sets_clean_exit_intent() -> None:
    """Listener returns without ever firing on_connected (e.g. gateway
    closed mid-handshake): exit_intent is still 'clean'."""
    creds = _make_creds()
    sf = _make_session_factory()

    async def fake_listener(session_factory, *, creds, on_connected=None, **_):
        # Never calls on_connected — gateway closed before HELLO finished.
        return None

    job = DiscordListenerJob(creds, sf, listener_fn=fake_listener)
    await job.run()

    assert job.exit_intent == "clean"
    assert job.connection_status() == "stopped"


@pytest.mark.asyncio
async def test_run_raises_keeps_exit_intent_unset() -> None:
    """When the listener body raises, the job does NOT stamp exit_intent —
    the supervisor coerces it to 'crashed' (per LongRunningJob contract).

    The status MUST still return to 'stopped' via the finally block.
    """
    creds = _make_creds()
    sf = _make_session_factory()

    async def fake_listener(session_factory, *, creds, on_connected=None, **_):
        # Simulate a websocket exception mid-handshake.
        raise RuntimeError("simulated gateway disconnect")

    job = DiscordListenerJob(creds, sf, listener_fn=fake_listener)

    with pytest.raises(RuntimeError, match="simulated gateway disconnect"):
        await job.run()

    # exit_intent stays 'unset' — supervisor will coerce to 'crashed'.
    assert job.exit_intent == "unset"
    # finally block ensures the status reflects reality.
    assert job.connection_status() == "stopped"


@pytest.mark.asyncio
async def test_run_does_not_clobber_operator_stop_intent() -> None:
    """Codex review IMPORTANT #1: if the supervisor's cancel path stamped
    ``operator_stop`` BEFORE the listener returned cleanly (a race
    between operator-click and gateway-close), the job's ``run()`` must
    leave the operator_stop intent in place — not overwrite it to
    ``clean``.
    """
    creds = _make_creds()
    sf = _make_session_factory()

    async def fake_listener(session_factory, *, creds, on_connected=None, **_):
        # Simulate the race: cancel-path runs while the listener is
        # mid-call. ``cancel_long_running`` would have stamped
        # operator_stop on the job before cancelling its task; we model
        # that by writing the intent directly. Then return cleanly
        # (gateway closed itself first, beating the supervisor's
        # task.cancel).
        job._exit_intent = "operator_stop"
        return None

    job = DiscordListenerJob(creds, sf, listener_fn=fake_listener)
    await job.run()

    # The operator-stop intent MUST survive — operator's intent wins
    # over the voluntary-clean default.
    assert job.exit_intent == "operator_stop"
    assert job.connection_status() == "stopped"


@pytest.mark.asyncio
async def test_run_missing_creds_fast_clean_exit() -> None:
    """``creds=None`` → run() returns immediately with exit_intent='clean'
    and ``connection_status`` never leaves 'stopped'. The listener body
    is NOT invoked.
    """
    sf = _make_session_factory()

    listener_called = False

    async def fake_listener(*args, **kwargs):
        nonlocal listener_called
        listener_called = True

    job = DiscordListenerJob(None, sf, listener_fn=fake_listener)
    await job.run()

    assert listener_called is False
    assert job.connection_status() == "stopped"
    assert job.exit_intent == "clean"


# ---------------------------------------------------------------------------
# Integration — supervisor sees crashed vs clean correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervisor_records_ok_on_clean_exit(engine: None) -> None:
    """End-to-end via the supervisor: a clean-exit listener leaves
    exactly one ``status='ok'`` audit row + DOES NOT restart.
    """
    creds = _make_creds()
    sf = _make_session_factory()

    async def fake_listener(session_factory, *, creds, on_connected=None, **_):
        if on_connected is not None:
            on_connected()
        return None

    job = DiscordListenerJob(creds, sf, listener_fn=fake_listener)
    reg = JobRegistry()
    reg.register(job=job, metadata=discord_listener_metadata())

    await reg.start_supervisors()

    task = reg._supervisor_tasks.get("discord_listener")
    assert task is not None
    await asyncio.wait_for(task, timeout=2.0)

    assert job.exit_intent == "clean"
    assert job.connection_status() == "stopped"

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "discord_listener")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "ok"
        assert rows[0].manual_trigger == 0
        assert rows[0].triggered_by.startswith("supervisor")

    await reg.stop_supervisors()


@pytest.mark.asyncio
async def test_supervisor_records_error_on_crash(engine: None) -> None:
    """A crashing listener → supervisor records status='error' +
    coerces exit_intent to 'crashed' + applies exp-backoff restart.
    """
    creds = _make_creds()
    sf = _make_session_factory()

    call_count = {"n": 0}

    async def fake_listener(session_factory, *, creds, on_connected=None, **_):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError(f"boom #{call_count['n']}")
        # After two crashes, return cleanly to terminate the supervisor.
        return None

    job = DiscordListenerJob(creds, sf, listener_fn=fake_listener)
    reg = JobRegistry()
    reg.register(job=job, metadata=discord_listener_metadata())

    # Fake clock so we don't wait real backoff.
    delays_observed: list[float] = []

    async def _fake_sleep(delay_s: float) -> None:
        delays_observed.append(delay_s)
        await asyncio.sleep(0)

    reg._sleep = _fake_sleep  # type: ignore[assignment]

    await reg.start_supervisors()
    task = reg._supervisor_tasks.get("discord_listener")
    assert task is not None
    await asyncio.wait_for(task, timeout=5.0)

    assert call_count["n"] == 3
    # Two crashes → two backoff sleeps.
    assert delays_observed == [1.0, 2.0]

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun)
                .where(JobRun.job_name == "discord_listener")
                .order_by(JobRun.id)
            )
        ).scalars().all()
        assert [r.status for r in rows] == ["error", "error", "ok"]
        assert all(
            r.error_message and "boom" in r.error_message for r in rows[:2]
        )

    await reg.stop_supervisors()


@pytest.mark.asyncio
async def test_supervisor_missing_creds_records_ok_no_restart(
    engine: None,
) -> None:
    """``creds=None`` registered with the supervisor: one ``status='ok'``
    audit row, no restart loop (per spec §3 IMPORTANT #3 — clean exit
    does NOT auto-restart in v1).
    """
    sf = _make_session_factory()

    listener_calls = {"n": 0}

    async def fake_listener(*args, **kwargs):
        listener_calls["n"] += 1

    job = DiscordListenerJob(None, sf, listener_fn=fake_listener)
    reg = JobRegistry()
    reg.register(job=job, metadata=discord_listener_metadata())

    await reg.start_supervisors()
    task = reg._supervisor_tasks.get("discord_listener")
    assert task is not None
    await asyncio.wait_for(task, timeout=2.0)

    # Listener body never invoked.
    assert listener_calls["n"] == 0
    # Connection status stayed at the initial 'stopped'.
    assert job.connection_status() == "stopped"
    assert job.exit_intent == "clean"

    async with db_mod.get_session() as s:
        rows = (
            await s.execute(
                select(JobRun).where(JobRun.job_name == "discord_listener")
            )
        ).scalars().all()
        # Exactly one cycle — clean exit, no restart.
        assert len(rows) == 1
        assert rows[0].status == "ok"

    await reg.stop_supervisors()
