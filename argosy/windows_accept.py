"""Process-local workaround for CPython issue 93821; keep Proactor subprocess IO.

CPython's Windows accept callback closes the *listener* on a client reset.
Retry only connection-reset accepts, closing each abandoned accepted socket.
Do not change the global interpreter, event-loop policy or non-accept IO.
Remove when the upstream fix is available in the supported Python runtime:
https://github.com/python/cpython/issues/93821
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import sys
from types import MethodType

_log = logging.getLogger(__name__)


def _begin_accept(proactor, listener, conn):
    # Same IOCP registration / accepted-context contract as CPython 3.12's
    # IocpProactor.accept; unlike its helper task, our task owns all failures.
    import _overlapped
    proactor._register_with_iocp(listener)
    overlapped = _overlapped.Overlapped(0)
    overlapped.AcceptEx(listener.fileno(), conn.fileno())

    def finish(transferred, key, operation):
        operation.getresult()
        conn.setsockopt(socket.SOL_SOCKET, _overlapped.SO_UPDATE_ACCEPT_CONTEXT,
                        struct.pack("@P", listener.fileno()))
        conn.settimeout(listener.gettimeout())
        return conn, conn.getpeername()

    return proactor._register(overlapped, listener, finish)


async def _accept_connected(proactor, listener):
    while True:
        conn = proactor._get_accept_socket(listener.family)
        try:
            return await _begin_accept(proactor, listener, conn)
        except OSError as exc:
            conn.close()
            if (getattr(exc, "winerror", None) not in (64, 10054)
                    or listener.fileno() == -1 or proactor._loop.is_closed()):
                raise
            _log.warning("windows.accept_client_reset_retry winerror=%s", exc.winerror)
            # Back off even on synchronous failures: never spin the event loop.
            await asyncio.sleep(0.05)
        except BaseException:
            conn.close()
            raise


def _retrying_accept(proactor, listener):
    return proactor._loop.create_task(_accept_connected(proactor, listener))


def install_accept_retry(loop=None):
    """Idempotent, Windows/IOCP-only; call during ASGI startup, before listen."""
    if sys.platform != "win32":
        return False
    from asyncio.windows_events import IocpProactor
    loop = loop or asyncio.get_running_loop()
    proactor = getattr(loop, "_proactor", None)
    if not isinstance(proactor, IocpProactor):
        return False
    if not getattr(proactor, "_argosy_accept_retry", False):
        proactor.accept = MethodType(_retrying_accept, proactor)
        proactor._argosy_accept_retry = True
    return True
