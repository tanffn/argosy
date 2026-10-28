"""A private Windows event loop for non-interactive SDK subprocesses.

Do not patch asyncio/anyio globally: interactive tools outside Argosy's model
worker must retain their normal console behavior. Both the SDK version probe
and its streaming process use this loop's public subprocess APIs.
"""
import asyncio
import subprocess
import sys


def new_background_loop():
    if sys.platform != "win32":
        return asyncio.new_event_loop()

    class BackgroundProactorLoop(asyncio.ProactorEventLoop):
        async def subprocess_exec(self, protocol_factory, program, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            return await super().subprocess_exec(protocol_factory, program, *args, **kwargs)

        async def subprocess_shell(self, protocol_factory, cmd, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            return await super().subprocess_shell(protocol_factory, cmd, **kwargs)

    return BackgroundProactorLoop()
