"""Exercise real Windows process creation, not just mocked SDK options."""
import asyncio
import subprocess
import sys

import anyio
import pytest

from argosy.services.background_loop import new_background_loop


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console regression")
def test_background_anyio_process_has_no_console_and_preserves_pipes(tmp_path):
    async def probe():
        process = await anyio.open_process(
            [sys.executable, "-c",
             "import ctypes,sys,os; print(ctypes.windll.kernel32.GetConsoleWindow()); "
             "print(os.environ['ARGOSY_PROBE']); print(sys.stdin.readline().strip()); "
             "print('stderr-ok',file=sys.stderr)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=tmp_path, env={"ARGOSY_PROBE": "env-ok"},
        )
        async with process:
            await process.stdin.send(b"stdin-ok\n")
            await process.stdin.aclose()
            stdout = b""
            async for chunk in process.stdout:
                stdout += chunk
            stderr = b""
            async for chunk in process.stderr:
                stderr += chunk
            assert await process.wait() == 0
        assert stdout.decode().splitlines() == ["0", "env-ok", "stdin-ok"]
        assert b"stderr-ok" in stderr

    loop = new_background_loop()
    try:
        loop.run_until_complete(probe())
    finally:
        loop.close()


def test_non_windows_factory_does_not_change_global_loop_policy(monkeypatch):
    import argosy.services.background_loop as module
    sentinel = object()
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(asyncio, "new_event_loop", lambda: sentinel)
    assert module.new_background_loop() is sentinel
