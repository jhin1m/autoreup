"""Agent runtime primitives: config + errors shared by all adapters.

Kept dependency-free so adapters (agy, command) can import without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


class AgentError(RuntimeError):
    """Agent invocation failed (spawn, timeout, non-success status)."""


@dataclass(slots=True)
class AgentConfig:
    """One headless agent call configuration.

    ``provider``:
      - ``"agy"``      — Antigravity CLI headless (``agy -p … --output-format json``)
      - ``"command"``  — any headless CLI template (ACP clients, claude/codex CLIs)

    ``home`` overrides ``HOME`` for the child process (account isolation).
    """

    provider: str = "agy"
    model: str = ""
    effort: str = ""
    bin: str = "agy"
    home: str | None = None
    command: str = ""
    timeout: float = 300.0
    retries: int = 3
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    def with_model(self, model: str) -> "AgentConfig":
        return replace(self, model=model)


__all__ = ["AgentConfig", "AgentError"]
