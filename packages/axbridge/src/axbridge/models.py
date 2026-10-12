"""JSON-friendly observations shared by the reader and executor."""

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Node:
    id: str
    role: str
    name: str = ""
    value: str | None = None
    enabled: bool | None = None
    focused: bool | None = None
    bounds: tuple[float, float, float, float] | None = None
    parent_id: str | None = None


@dataclass
class Snapshot:
    app_pid: int
    window_title: str
    captured_at: str
    nodes: list[Node]
    url: str | None = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class BridgeError(RuntimeError):
    """An actionable failure that the CLI should report without a traceback."""
