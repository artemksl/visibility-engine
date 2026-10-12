"""Borrow the shared async executor while retaining runner-owned admission.

The runner remains responsible for its send journal, provider contracts and
response correlation.  This adapter supplies only guarded input to an existing
owned page; it never connects a browser, discovers a profile or retries input.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
import sys
import random
from typing import Any

from automation_core.browser.async_actions import AsyncActionExecutor
from automation_core.browser.movement import Policy
from automation_core.contracts import SensitiveValue


@dataclass(frozen=True)
class RetainedActionTarget:
    locator: Any
    native: Any


class SharedBrowserActions:
    def __init__(self, submission):
        self.submission = submission
        self.executor = AsyncActionExecutor(
            submission.page,
            assert_owned=self.assert_owned,
            cancelled=submission.cancelled,
            movement_policy=Policy(seed=random.SystemRandom().getrandbits(64), target_mode="centroid"),
        )
        self.targets: list[RetainedActionTarget] = []

    async def assert_owned(self, deadline: float) -> bool:
        # The attached identity is issued only by OwnedTabRegistry.  Its local
        # checks run at every input guard; the source verifier also re-proves
        # the live CDP target and conversation at the actual send boundary.
        from ..websites import chat_actions as chat

        submission = self.submission
        if time.monotonic() >= deadline:
            raise chat.BrowserInteractionError("shared action deadline expired")
        bound = chat._runner_owned_page_identity(
            submission.page,
            profile_id=submission.profile_id,
            chrome_directory=submission.chrome_directory,
        )
        if bound is None:
            raise chat.IdentityMismatchError("browser action page is not runner-owned")
        observed = chat.BrowserIdentity(
            profile_id=submission.profile_id,
            chrome_directory=submission.chrome_directory,
            browser_context_reference=chat._identity_value(bound, "browser_context_reference"),
            tab_reference=chat._identity_value(bound, "tab_reference"),
            browser_target_reference=chat._identity_value(bound, "browser_target_reference"),
            conversation_reference=str(getattr(submission.page, "url", "") or ""),
        )
        submission.identity.assert_same_tab(observed)
        return True

    async def retain(self, locator, deadline: float) -> RetainedActionTarget:
        target = RetainedActionTarget(locator, await self.executor.retain(locator, deadline))
        self.targets.append(target)
        return target

    async def validate(self, target: RetainedActionTarget, deadline: float) -> None:
        await self.executor.validate(target.native, deadline)

    async def release_all(self) -> None:
        targets, self.targets = self.targets, []
        for target in targets:
            await self.executor.release(target.native)

    async def clear(self, target: RetainedActionTarget, deadline: float, verify) -> None:
        # Target-local keyboard input preserves each provider's editor events.
        await self.executor.press(target.native, "Meta+A" if sys.platform == "darwin" else "Control+A", deadline, verify=verify)
        await self.executor.press(target.native, "Backspace", deadline, verify=verify)

    async def write(self, target: RetainedActionTarget, text: str, deadline: float,
                    verify, *, sequential: bool, min_delay_ms=0., max_delay_ms=0.) -> None:
        sensitive = SensitiveValue(text)
        try:
            if sequential:
                await self.executor.type_text(target.native, sensitive, deadline,
                    verify=verify, min_delay_ms=min_delay_ms, max_delay_ms=max_delay_ms)
            else:
                await self.executor.fill(target.native, sensitive, deadline, verify=verify)
        finally:
            sensitive.clear()

    async def click(self, target: RetainedActionTarget, deadline: float, verify):
        return await self.executor.click(target.native, deadline, verify=verify)

    async def press(self, target: RetainedActionTarget, key: str, deadline: float, verify):
        return await self.executor.press(target.native, key, deadline, verify=verify)
