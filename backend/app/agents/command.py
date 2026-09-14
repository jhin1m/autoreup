"""Generic headless command adapter.

Used for any agent that is not Antigravity: ACP clients (``acpx <agent> …``),
``claude -p``, ``codex exec``, … Configure with a command template; ``{prompt}``
is substituted if present, otherwise the prompt is appended as the final argv.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from .config import AgentConfig, AgentError


def build_command_argv(template: str, prompt: str) -> list[str]:
    parts = shlex.split(template)
    if not parts:
        raise AgentError("agent_command is empty")
    if any("{prompt}" in part for part in parts):
        return [part.replace("{prompt}", prompt) for part in parts]
    return [*parts, prompt]


def run_command(prompt: str, config: AgentConfig) -> str:
    env = os.environ.copy()
    if config.home:
        env["HOME"] = os.path.expanduser(config.home)
    argv = build_command_argv(config.command, prompt)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=config.timeout, env=env)
    except FileNotFoundError as exc:
        raise AgentError(f"agent command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"agent command timed out after {config.timeout}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise AgentError(f"agent command exited {proc.returncode}: {detail[-1] if detail else 'no output'}")
    return proc.stdout.strip()


__all__ = ["build_command_argv", "run_command"]
