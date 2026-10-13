"""Advancing clock fixture for response polling deadline tests."""
from __future__ import annotations
import asyncio
from types import SimpleNamespace
import pytest
from visibility_runner.websites import chat_actions as base
from visibility_runner.websites import page_readers
from visibility_runner.browser import operations


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    # Replace only this module's references, not asyncio/time process-wide.
    local_asyncio = SimpleNamespace(**vars(asyncio))
    local_asyncio.sleep = value.sleep
    for module in (base, page_readers, operations):
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=value.monotonic))
        monkeypatch.setattr(module, "asyncio", local_asyncio)
    return value
