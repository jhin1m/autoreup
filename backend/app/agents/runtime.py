"""Headless CLI-agent runtime — the single text-generation seam.

AutoReup does not call LLM APIs. Every text step (glossary extraction, translation,
repair, naming) goes through a headless agent CLI: Antigravity (``agy``) natively,
or any ACP/headless command via a configurable template.

The deterministic pipeline still owns timing, segmentation and QA; the agent only
receives a bounded prompt and returns text.
"""

from __future__ import annotations

import time

from .agy import run_agy
from .command import run_command
from .config import AgentConfig, AgentError


def compose_prompt(system: str, user: str) -> str:
    system = system.strip()
    if not system:
        return user
    return f"{system}\n\n---\n\n{user}"


def run_once(prompt: str, config: AgentConfig) -> str:
    provider = (config.provider or "agy").strip().lower()
    if provider in {"agy", "antigravity"}:
        return run_agy(prompt, config)
    if provider in {"command", "cli", "acp"}:
        if not config.command.strip():
            raise AgentError("provider=command requires agent_command (template with {prompt})")
        return run_command(prompt, config)
    raise AgentError(f"unknown agent provider: {config.provider!r}")


def complete(system: str, user: str, config: AgentConfig) -> str:
    """Run one agent call with retries; raise AgentError after the last attempt."""
    prompt = compose_prompt(system, user)
    last_error: Exception | None = None
    for attempt in range(max(1, config.retries)):
        try:
            return run_once(prompt, config)
        except Exception as exc:  # noqa: BLE001 - spawn errors vary by adapter
            last_error = exc
            if attempt + 1 < config.retries:
                time.sleep(0.5 * (attempt + 1))
    raise AgentError(f"agent call failed after {config.retries} attempts: {last_error}")


__all__ = ["AgentConfig", "AgentError", "complete", "compose_prompt", "run_once"]
