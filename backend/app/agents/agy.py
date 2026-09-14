"""Antigravity CLI (``agy``) headless adapter.

One-shot print mode: ``agy -p <prompt> --output-format json``. Text prompts avoid
shell quoting entirely because argv is passed directly (no shell). ``--print-timeout``
is the CLI-side ceiling; the subprocess timeout is a hard backstop.
"""

from __future__ import annotations

import json
import os
import subprocess

from .config import AgentConfig, AgentError


def format_timeout(seconds: float) -> str:
    total = max(1, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    return f"{minutes}m{secs}s"


def build_agy_argv(prompt: str, config: AgentConfig) -> list[str]:
    argv = [
        config.bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--print-timeout",
        format_timeout(config.timeout),
    ]
    if config.model:
        argv += ["--model", config.model]
    if config.effort:
        argv += ["--effort", config.effort]
    argv += [str(arg) for arg in config.extra_args]
    return argv


def parse_agy_output(stdout: str) -> str:
    """Extract ``response`` from the last JSON result envelope on stdout."""
    for raw in reversed(stdout.splitlines()):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "status" not in data:
            continue
        if data.get("status") != "SUCCESS":
            raise AgentError(str(data.get("error") or f"agent status {data.get('status')}"))
        return str(data.get("response") or "")
    raise AgentError("agent returned no result envelope")


def run_agy(prompt: str, config: AgentConfig) -> str:
    env = os.environ.copy()
    if config.home:
        env["HOME"] = os.path.expanduser(config.home)
    argv = build_agy_argv(prompt, config)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=config.timeout + 30, env=env)
    except FileNotFoundError as exc:
        raise AgentError(f"agent binary not found: {config.bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"agent timed out after {config.timeout}s") from exc
    if proc.returncode != 0 and not proc.stdout.strip():
        detail = (proc.stderr or "").strip().splitlines()
        raise AgentError(f"agent exited {proc.returncode}: {detail[-1] if detail else 'no output'}")
    return parse_agy_output(proc.stdout)


__all__ = ["build_agy_argv", "format_timeout", "parse_agy_output", "run_agy"]
