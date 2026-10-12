"""Small common contract used by mailbox transports and verification logic."""

from dataclasses import dataclass, field
from datetime import datetime
import math
import time
from typing import Callable, Protocol


class MailAccessError(RuntimeError):
    """A sanitized error safe to display; never include provider response bodies."""


class MailSignInRequired(MailAccessError):
    """Cached authorization is unavailable or explicitly requires user sign-in."""


class NeedsAttention(RuntimeError):
    """Automatic handling stopped because continuing would require a guess."""


class BudgetStopped(NeedsAttention, MailAccessError):
    """A sanitized cooperative timing/admission cause, preserved by adapters."""

    def __init__(self, reason="deadline_exhausted"):
        allowed = {"deadline_exhausted", "cancelled", "ownership_lost", "storage_unavailable",
                   "completion_only", "clock_untrusted", "budget_denied"}
        self.reason = reason if reason in allowed else "budget_denied"
        super().__init__("The shared operation budget reached its timeout or denied admission; automatic handling stopped.")


class OperationBudget(Protocol):
    """Borrowed permission; checkpoint returns remaining seconds or raises.

    Runtime CallBudget satisfies this protocol. This interface does not interrupt
    a blocking dependency and does not grant permission to repeat an effect.
    """
    def checkpoint(self) -> float: ...


def check_budget(budget: OperationBudget | None) -> float:
    if budget is None:
        return math.inf
    try:
        remaining = budget.checkpoint()
    except BudgetStopped:
        raise
    except Exception as error:
        raise BudgetStopped(getattr(error, "reason", "budget_denied")) from None
    if (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining) or remaining < .001):
        raise BudgetStopped()
    return remaining


def timeout_seconds(budget: OperationBudget | None, cap: float) -> float:
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap < .001:
        raise ValueError("A positive finite timeout is required.")
    return min(cap, check_budget(budget))


def timeout_milliseconds(budget: OperationBudget | None, cap: float) -> int:
    if isinstance(cap, bool) or not isinstance(cap, (int, float)):
        raise ValueError("A positive finite timeout is required.")
    return math.floor(timeout_seconds(budget, cap / 1000) * 1000)


@dataclass(frozen=True)
class BudgetWindow:
    """One local deadline clipped by a borrowed parent; never a restart budget."""
    until: float
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    parent: OperationBudget | None = field(default=None, repr=False)

    def __post_init__(self):
        if (isinstance(self.until, bool) or not isinstance(self.until, (int, float))
                or not math.isfinite(self.until) or self.until < 0):
            raise ValueError("A finite monotonic deadline is required.")

    @classmethod
    def start(cls, timeout: float, *, monotonic=time.monotonic, parent=None):
        timeout_seconds(None, timeout)
        check_budget(parent)
        return cls(monotonic() + timeout, monotonic, parent)

    def checkpoint(self) -> float:
        remaining = min(self.until - self.monotonic(), check_budget(self.parent))
        if remaining < .001:
            raise BudgetStopped()
        return remaining

    def timeout_s(self, cap_s=None):
        return check_budget(self) if cap_s is None else timeout_seconds(self, cap_s)

    def timeout_ms(self, cap_ms=None):
        return math.floor(check_budget(self) * 1000) if cap_ms is None else timeout_milliseconds(self, cap_ms)


@dataclass(frozen=True)
class MailMessage:
    provider_id: str = field(repr=False)
    sender: str
    recipients: tuple[str, ...]
    sent_at: datetime | None
    received_at: datetime
    subject: str = field(repr=False)
    text: str = field(repr=False)
    internet_id: str = field(default="", repr=False)


class MailProvider(Protocol):
    """Read-only adapters return a complete, bounded time window or raise.

    Adapters must cover all available message folders, preserve code formatting,
    and sanitize exceptions. Access credentials and raw messages stay out of logs.
    """

    def collect(self, since: datetime) -> list[MailMessage]: ...


class BudgetedMailProvider(Protocol):
    """Explicit cooperative adapter, separate from the legacy collect contract."""
    def collect_with_budget(self, since: datetime, *, budget: OperationBudget) -> list[MailMessage]: ...


class BudgetedRequest(Protocol):
    def __call__(self, *, budget: OperationBudget) -> None: ...


class BudgetedSubmission(Protocol):
    def __call__(self, code: str, *, budget: OperationBudget): ...
