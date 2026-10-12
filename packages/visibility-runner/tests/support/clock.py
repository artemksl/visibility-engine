"""Manual polling clock; the event loop and its watchdog keep real time."""
from __future__ import annotations

import asyncio


class _ModuleProxy:
    def __init__(self, module, **overrides):
        self._module = module
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._module, name)


class PollClock:
    """Step one polling loop only after it has reached its next sleep barrier."""

    def __init__(self):
        self.now = 0.0
        self._scheduled = asyncio.Queue()
        self._pending = None

    def monotonic(self):
        return self.now

    async def sleep(self, delay, result=None):
        ready = asyncio.get_running_loop().create_future()
        self._scheduled.put_nowait((delay, ready))
        await ready
        return result

    async def next_sleep(self):
        assert self._pending is None, "advance the current sleep before waiting for another"
        self._pending = await self._scheduled.get()
        return self._pending[0]

    def advance(self):
        assert self._pending is not None, "wait for the poller's sleep barrier first"
        delay, ready = self._pending
        self._pending = None
        self.now = round(self.now + delay, 12)
        ready.set_result(None)

    def patch(self, monkeypatch, module):
        # Replace the target's module references, never asyncio.sleep or
        # time.monotonic globally: asyncio timeouts must remain independent.
        monkeypatch.setattr(module, "asyncio", _ModuleProxy(module.asyncio, sleep=self.sleep))
        if hasattr(module, "time"):
            monkeypatch.setattr(module, "time", _ModuleProxy(module.time, monotonic=self.monotonic))
