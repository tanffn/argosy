"""``DiscordListenerJob`` — Sprint A commit #6.

Wraps :func:`argosy.services.discord_listener.run_discord_listener` as a
:class:`~argosy.orchestrator.loops.base.LongRunningJob` so the
:class:`~argosy.services.jobs.registry.JobRegistry` supervisor (commit
#5) owns the (connect, disconnect) cycle, the exponential-backoff
restart state, and the ``job_runs`` audit rows.

Retires the external-cron expectation that earlier shipped with the
listener — production now goes through the supervisor; ``argosy
discord-ingest`` is one-shot smoke only.

Lifecycle of one supervisor cycle:

1. Supervisor opens a ``job_runs`` row in ``status='running'``.
2. Calls :meth:`DiscordListenerJob.run`.
3. ``run()`` flips ``_status`` to ``"reconnecting"`` and awaits
   ``run_discord_listener``, passing an ``on_connected`` callback that
   flips ``_status`` to ``"connected"`` once the gateway HELLO has been
   acked and heartbeat is scheduled.
4. ``finally``: ``_status`` returns to ``"stopped"``;
   ``exit_intent`` is stamped (``"clean"`` on normal return; the
   supervisor coerces a raise into ``"crashed"``).

Missing-creds path
------------------

If credentials are missing at construction (``creds=None``) the job
``run()`` does a fast-clean exit: logs once, stamps
``exit_intent='clean'``, leaves ``_status='stopped'``. The supervisor
sees a clean exit and does NOT auto-restart (per spec §3 IMPORTANT #3).
The job remains registered so the operator can see "creds missing;
drop ~/.argosy/discord_creds.json to activate" in the admin UI rather
than the row disappearing.

Race notes (spec §6 codex review focus)
---------------------------------------

* The supervisor opens a ``job_runs`` row BEFORE ``run()`` flips
  ``_status`` to ``"reconnecting"``. There is a small window where the
  raw ``job_runs.status='running'`` disagrees with
  ``connection_status()='stopped'``. The :class:`JobRegistry.list`
  health derivation explicitly prefers ``connection_status()`` for
  ``LongRunningJob`` (see ``registry.py`` near
  ``isinstance(rec.job, LongRunningJob)``), so the UI sees a coherent
  state even during the window.
* The ``on_connected`` callback fires inside
  :func:`run_discord_listener` immediately AFTER
  ``await client.connect()`` returns — the real client returns from
  ``connect()`` once the gateway HELLO has been received, IDENTIFY has
  been SENT, and the heartbeat task has been scheduled. The gateway's
  IDENTIFY-ACK / ``READY`` dispatch arrives LATER inside the messages
  loop. So the callback semantics are GATEWAY-TRANSPORT-CONNECTED, NOT
  authenticated (codex review BLOCKER on commit #6). The trade-off:
  green-dot lights up promptly on a healthy gateway; on a revoked
  token there's a brief false-green window before the gateway closes
  the connection. A stricter "fire on first READY" semantic is a
  follow-on if false-greens become operator-visible.

The supervisor handles cancellation: :meth:`cancel` defers to the
listener's own ``finally``-block close (``client.close()`` is in a
``finally`` inside ``run_discord_listener``), so the supervisor's
``task.cancel()`` is sufficient to unwind a connected listener.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.orchestrator.loops.base import ConnectionStatus, LongRunningJob
from argosy.services.discord_listener import (
    DiscordCreds,
    run_discord_listener,
)
from argosy.services.jobs.registry import JobMetadata

_log = get_logger("argosy.jobs.discord_listener")

# Discord gateway close codes that reconnecting CANNOT fix — auth/config
# failures. Retrying just hammers the gateway, and a tight reconnect storm is
# exactly what gets a token rate-limited / blocked. Treat these like missing
# creds: stop cleanly, NO supervisor restart, leave the job visible in the admin
# UI until the operator refreshes credentials.
#   4004 auth failed | 4010 invalid shard | 4011 sharding required
#   4012 invalid API version | 4013 invalid intent(s) | 4014 disallowed intent(s)
_TERMINAL_DISCORD_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})


def _terminal_close_code(exc: BaseException) -> int | None:
    """Return the non-recoverable Discord close code behind a websocket
    disconnect (across websockets-library exception shapes), or None when the
    failure is transient and a reconnect is warranted."""
    candidates: list[object] = [getattr(exc, "code", None)]
    for frame_attr in ("rcvd", "sent"):
        frame = getattr(exc, frame_attr, None)
        if frame is not None:
            candidates.append(getattr(frame, "code", None))
    for c in candidates:
        try:
            ci = int(c)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if ci in _TERMINAL_DISCORD_CLOSE_CODES:
            return ci
    # Last resort: the code is reliably embedded in the message text, e.g.
    # "received 4004 (private use) Authentication failed".
    import re

    m = re.search(r"\b(4004|401[0-4])\b", str(exc))
    if m and int(m.group(1)) in _TERMINAL_DISCORD_CLOSE_CODES:
        return int(m.group(1))
    return None


def discord_listener_metadata() -> JobMetadata:
    """Construct the :class:`JobMetadata` row for the registry.

    Imported by the ``argosy/api/main.py`` startup hook's guarded-import
    block; the registration call happens there. Source kind is
    ``ingest`` per the spec's §6 mapping table.
    """
    return JobMetadata(
        name="discord_listener",
        schedule_cron=None,  # long-running, no cron
        schedule_human="long-running (supervised)",
        source_kind="ingest",
        description=(
            "Discord gateway listener that ingests news messages into "
            "the news_signals table. Supervised by JobRegistry — restarts "
            "on crash with exponential backoff."
        ),
        long_running=True,
    )


# Type alias kept narrow — the listener's body accepts a sync
# sessionmaker-like callable; we don't constrain further so test doubles
# (an in-memory ``sessionmaker``) plug in cleanly.
SessionFactory = Callable[[], Session]

# Type alias for the listener body — injectable so tests can swap in a
# fake without monkey-patching the module.
ListenerFn = Callable[..., object]


class DiscordListenerJob(LongRunningJob):
    """Wraps :func:`run_discord_listener` as a supervised long-running job.

    Constructor:

        :param creds: Validated :class:`DiscordCreds`, or ``None`` when
            credentials are missing. ``None`` → ``run()`` fast-exits
            cleanly so the supervisor records a single ``status='ok'``
            row + does NOT restart (per spec §3 IMPORTANT #3).
        :param session_factory: Zero-arg callable returning a sync
            SQLAlchemy ``Session``. Passed directly to
            ``run_discord_listener``.
        :param listener_fn: Optional override for the listener body.
            Default :func:`run_discord_listener`. Tests inject a stub so
            we don't hit a real Discord gateway.
    """

    name = "discord_listener"

    def __init__(
        self,
        creds: DiscordCreds | None,
        session_factory: SessionFactory,
        *,
        listener_fn: ListenerFn | None = None,
    ) -> None:
        super().__init__()
        self._creds = creds
        self._session_factory = session_factory
        self._listener_fn = listener_fn or run_discord_listener
        self._status: ConnectionStatus = "stopped"

    def connection_status(self) -> ConnectionStatus:
        """Fast read; supervisor's :meth:`JobRegistry.list` polls this
        every render of the admin UI's jobs table."""
        return self._status

    async def run(self) -> None:
        """Long-lived body: connect, listen until the gateway disconnects.

        Sets ``_exit_intent='clean'`` on normal return — UNLESS the
        supervisor's cancel path already stamped ``'operator_stop'``
        (which it does BEFORE cancelling our task; see
        :meth:`JobRegistry.cancel_long_running`). In that case we leave
        the operator-stop intent alone (codex review IMPORTANT #1 —
        without this guard a clean listener return that happens to race
        a cancel would clobber the operator-stop label and make the
        audit row look like a voluntary exit).

        Exceptions propagate to the supervisor, which coerces them to
        ``'crashed'`` and applies exp-backoff restart.

        Cancellation: ``await task.cancel()`` from
        :meth:`JobRegistry.cancel_long_running` raises
        :class:`asyncio.CancelledError` inside ``run_discord_listener``'s
        ``async for``; the listener's own ``finally`` block closes the
        client. We let the CancelledError propagate so the supervisor
        sees ``operator_stop`` (which it stamped before cancelling) and
        closes the audit row with ``status='cancelled'``.
        """
        # Missing-creds fast-exit. The supervisor sees a clean return
        # and does NOT auto-restart per spec §3 IMPORTANT #3. The job
        # remains registered so the admin UI shows "creds missing".
        #
        # Audit-row caveat (codex review IMPORTANT #2): the supervisor
        # records this cycle as ``status='ok'`` with
        # ``output_summary={"notes": "clean exit"}``. The
        # ``connection_status()`` staying ``"stopped"`` is the visible
        # symptom in the admin UI — health derivation maps ``stopped``
        # to ``red``, so the operator's primary signal is health-red,
        # not the audit-row status. We log at WARNING (not INFO) so
        # the missing-creds case stands out in tail-the-log workflows.
        # A future spec revision could thread a job-level
        # ``output_summary`` through the supervisor for finer
        # attribution without changing the registry's single-writer
        # contract.
        if self._creds is None:
            _log.warning(
                "discord_listener.run.missing_creds",
                note="dormant; drop ~/.argosy/discord_creds.json to activate",
            )
            self._status = "stopped"
            self._exit_intent = "clean"
            return

        # Status flip to 'reconnecting' BEFORE awaiting the listener so
        # the admin UI sees the transition (the supervisor's audit row
        # opens before this point — see module docstring race notes).
        self._status = "reconnecting"
        try:
            await self._listener_fn(
                self._session_factory,
                creds=self._creds,
                on_connected=self._on_gateway_connected,
            )
            # Normal return: gateway closed cleanly OR the message
            # iterator stopped. Either way, this is a clean exit per
            # the listener's contract — no auto-restart.
            #
            # Codex review IMPORTANT #1: don't clobber a pre-stamped
            # ``operator_stop``. ``JobRegistry.cancel_long_running``
            # writes ``_exit_intent='operator_stop'`` BEFORE cancelling
            # our task. If the cancel landed AFTER
            # ``run_discord_listener`` already returned (a clean-close
            # race), the stamp is sitting here. Leave it alone —
            # operator intent wins over the voluntary-clean default.
            if self._exit_intent != "operator_stop":
                self._exit_intent = "clean"
        except Exception as exc:  # noqa: BLE001 — terminal-auth triage before re-raise
            # A non-recoverable gateway close (4004 auth / 401x config) must NOT
            # become a supervisor crash→restart: the reconnect storm is what
            # blocks the token. Stop cleanly (no restart), like missing creds.
            # CancelledError is BaseException, so operator-stop still propagates.
            code = _terminal_close_code(exc)
            if code is None:
                raise  # transient → let the supervisor restart with backoff
            _log.error(
                "discord_listener.run.terminal_close",
                close_code=code,
                note=(
                    "Discord gateway closed with a non-recoverable code; stopping "
                    "the listener WITHOUT auto-restart until credentials are "
                    "refreshed (tight reconnects can get the token blocked)."
                ),
            )
            self._exit_intent = "clean"
        finally:
            # Always return to 'stopped' on exit (clean, operator_stop,
            # or crashed). The supervisor reads exit_intent (set above
            # or coerced on raise) to decide restart semantics.
            self._status = "stopped"

    def _on_gateway_connected(self) -> None:
        """Callback fired by ``run_discord_listener`` after the gateway
        transport handshake completes (HELLO received + IDENTIFY sent +
        heartbeat scheduled).

        Flips ``_status`` to ``"connected"`` so the admin UI's health
        derivation lights up green. NOTE — this is the
        GATEWAY-TRANSPORT-CONNECTED point, not authenticated. See the
        module docstring's BLOCKER note for the deliberate trade-off
        between prompt UI greening and a brief false-green window on
        revoked tokens.
        """
        self._status = "connected"


__all__ = [
    "DiscordListenerJob",
    "discord_listener_metadata",
]
