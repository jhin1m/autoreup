"""Workspace agent runs — file-first, for content-heavy work.

Verified with ``agy`` 1.2.x headless: print mode has no default workspace (register
one with ``--add-dir``), file tools are soft-denied unless
``--dangerously-skip-permissions`` is passed (pair with ``--sandbox``), and the CLI
can loop internally across many turns. So a translation job is ONE agent run that
reads inputs, writes its draft, reviews it against the source and writes the final
file — while the deterministic pipeline validates the artifacts FROM DISK, never
from model prose.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .agy import format_timeout
from .config import AgentConfig, AgentError

# Provider-specific flags that turn a one-shot CLI into a workspace agent.
_WORKSPACE_ARGS: dict[str, list[str]] = {
    "agy": ["--dangerously-skip-permissions", "--sandbox"],
}


@dataclass(slots=True)
class WorkspaceRun:
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False


def build_workspace_argv(prompt: str, config: AgentConfig, workspace: Path, *, resume: bool = False) -> list[str]:
    provider = (config.provider or "agy").strip().lower()
    if provider in {"command", "cli", "acp"}:
        template = config.command.strip()
        if not template:
            raise AgentError("provider=command requires agent_command (template with {prompt})")
        return [part.replace("{prompt}", prompt) for part in template.split()]
    argv = [
        config.bin,
        "-p",
        prompt,
        "--add-dir",
        str(workspace),
        "--print-timeout",
        format_timeout(config.timeout),
    ]
    argv += _WORKSPACE_ARGS.get(provider, [])
    if config.model:
        argv += ["--model", config.model]
    if config.effort:
        argv += ["--effort", config.effort]
    if resume:
        argv += ["--continue"]
    argv += [str(arg) for arg in config.extra_args]
    return argv


def run_agent_workspace(
    workspace: Path,
    prompt: str,
    config: AgentConfig,
    *,
    timeout: float | None = None,
    resume: bool = False,
) -> WorkspaceRun:
    """Run one agent turn (or loop) inside ``workspace``; never raises on timeout."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    limit = float(timeout if timeout is not None else config.timeout)
    argv = build_workspace_argv(prompt, config, workspace, resume=resume)
    env = os.environ.copy()
    if config.home:
        env["HOME"] = os.path.expanduser(config.home)
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(workspace), capture_output=True, text=True, timeout=limit + 30, env=env)
        return WorkspaceRun(proc.returncode, proc.stdout, proc.stderr, round(time.monotonic() - started, 1))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return WorkspaceRun(-1, stdout, "workspace agent timed out", round(time.monotonic() - started, 1), timed_out=True)
    except FileNotFoundError as exc:
        raise AgentError(f"agent binary not found: {config.bin}") from exc


__all__ = ["WorkspaceRun", "build_workspace_argv", "run_agent_workspace"]
