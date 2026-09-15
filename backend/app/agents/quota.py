"""Agent quota oracle — the shared resource of every agent stage.

``agy -p /usage`` is free and returns the exact remaining percentages per model pool
(verified: ``Gemini Models  Five Hour Limit Remaining  57%  <reset>``). Agent stages
share that pool with each other and with the fit loop, so parallelism must be chosen
against headroom, not guessed. Snapshots are cached briefly (the oracle is a CLI call,
and the numbers move slowly).
"""

from __future__ import annotations

import subprocess
import time

_CACHE: tuple[float, dict[str, float]] | None = None
_TTL = 600.0  # seconds — the oracle call is cheap but not free (agent startup)


def _parse_usage(text: str) -> dict[str, float]:
    """{'gemini_5h': 0.57, 'gemini_week': 0.73, 'claude_5h': 1.0, ...} as 0..1 shares."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("\t") if cell.strip()]
        if len(cells) < 3 or not cells[-2].endswith("%"):
            continue
        pool = cells[0].lower()
        window = cells[-2].replace(" Limit Remaining", "").lower().replace(" ", "_")
        try:
            share = float(cells[-2].removesuffix("%")) / 100.0
        except ValueError:
            continue
        key = ("gemini" if "gemini" in pool else "claude_gpt") + "_" + ("5h" if "five" in window else "week")
        out[key] = share
    return out


def usage_snapshot(binary: str = "agy", timeout: float = 45.0) -> dict[str, float]:
    """Cached quota snapshot; empty dict when the oracle is unavailable."""
    global _CACHE
    now = time.monotonic()
    if _CACHE and now - _CACHE[0] < _TTL:
        return _CACHE[1]
    try:
        proc = subprocess.run([binary, "-p", "/usage"], capture_output=True, text=True, timeout=timeout)
        parsed = _parse_usage(proc.stdout)
    except Exception:  # noqa: BLE001 - the oracle is advisory; absence just means "unknown"
        parsed = {}
    _CACHE = (now, parsed)
    return parsed


def gemini_headroom(binary: str = "agy") -> float | None:
    """Remaining share of the Gemini 5-hour window (0..1), or None when unknown."""
    return usage_snapshot(binary).get("gemini_5h")


__all__ = ["gemini_headroom", "usage_snapshot"]
