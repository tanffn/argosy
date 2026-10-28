"""Real Windows sockets/subprocesses; inject only the kernel-reset result."""
import asyncio
import socket
import sys

import pytest

import argosy.windows_accept as recovery

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows IOCP contract")


@pytest.mark.real_seam
def test_reset_retries_accept_without_losing_listener_or_subprocess_support(monkeypatch):
    original = recovery._begin_accept
    abandoned, errors = [], []
    def first_reset(proactor, listener, conn):
        if not abandoned:
            abandoned.append(conn)
            future = proactor._loop.create_future()
            future.set_exception(OSError(22, "Client reset", None, 64))
            return future
        return original(proactor, listener, conn)
    monkeypatch.setattr(recovery, "_begin_accept", first_reset)
    async def check():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        assert recovery.install_accept_retry()
        assert recovery.install_accept_retry()  # no double wrapper
        async def echo(reader, writer):
            writer.write(await reader.read(20))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            for _ in range(2):
                reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 3)
                writer.write(b"still-serving")
                await writer.drain()
                assert await asyncio.wait_for(reader.read(20), 3) == b"still-serving"
                writer.close()
                await writer.wait_closed()
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", "print('subprocess-ok')",
                                                           stdout=asyncio.subprocess.PIPE)
            output, _ = await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 0 and b"subprocess-ok" in output
        finally:
            server.close()
            await server.wait_closed()
            await asyncio.sleep(.05)
    with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
        runner.run(check())
    assert abandoned[0].fileno() == -1
    assert not errors


def test_fatal_accept_errors_are_not_hidden_and_pending_cancel_closes_socket(monkeypatch):
    sockets = []
    def broken(proactor, listener, conn):
        sockets.append(conn)
        raise OSError(5, "Not a client reset", None, 5)
    monkeypatch.setattr(recovery, "_begin_accept", broken)
    async def check():
        loop = asyncio.get_running_loop()
        recovery.install_accept_retry()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.setblocking(False)
            with pytest.raises(OSError, match="Not a client reset"):
                await recovery._accept_connected(loop._proactor, listener)
            assert listener.fileno() != -1
            def pending(proactor, listener, conn):
                sockets.append(conn)
                return loop.create_future()
            monkeypatch.setattr(recovery, "_begin_accept", pending)
            task = loop.create_task(recovery._accept_connected(loop._proactor, listener))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
        runner.run(check())
    assert len(sockets) == 2 and all(conn.fileno() == -1 for conn in sockets)
