"""Input seam for locator-only page fixtures; no real browser effects."""
import time
from types import SimpleNamespace

from visibility_runner.browser import input as browser_input
from visibility_runner.browser.input import type_composer_value
from visibility_runner.errors import BrowserInteractionError

class SyntheticBrowserActions:
    """Keep locator-only unit tests independent of the real owned-page driver.

    These tests exercise exact readback, retry fences and submission ordering.
    Real retained DOM targets and ownership/source verification belong to the
    shared-executor integration tests, rather than these synthetic composers.
    """

    def __init__(self, submission):
        self.submission = submission
        self.targets = []

    async def assert_owned(self, deadline):
        if time.monotonic() >= deadline:
            raise BrowserInteractionError("synthetic action deadline expired")
        if self.submission.cancelled():
            raise BrowserInteractionError("synthetic action cancelled")
        return True

    async def retain(self, locator, deadline):
        await self.assert_owned(deadline)
        target = SimpleNamespace(locator=locator)
        self.targets.append(target)
        return target

    async def validate(self, target, deadline):
        await self.assert_owned(deadline)
        assert any(retained is target for retained in self.targets)

    async def release_all(self):
        self.targets.clear()

    async def clear(self, target, deadline, verify):
        await self.validate(target, deadline)
        await browser_input.clear_composer(target.locator)

    async def write(self, target, text, deadline, verify, *, sequential,
                    min_delay_ms=0., max_delay_ms=0.):
        await self.validate(target, deadline)
        if sequential:
            await type_composer_value(target.locator, text,
                min_delay_ms=min_delay_ms, max_delay_ms=max_delay_ms)
        else:
            await browser_input.write_composer_value(target.locator, text)

    async def click(self, target, deadline, verify):
        await self.validate(target, deadline)
        return await target.locator.click()

    async def press(self, target, key, deadline, verify):
        await self.validate(target, deadline)
        return await target.locator.press(key)
