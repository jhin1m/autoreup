"""The code gate for agent edits (draft -> final, repair, expand).

LLM passes may rewrite a line; code decides whether the rewrite is provably safe.
A patch is accepted only when it cannot have changed the meaning: numbers, glossary
terms, Vietnamese negations and turn markers must survive, length must stay inside a
bounded ratio, and no Han character may appear. Everything the agent writes goes
through this gate — a bad edit can only be rejected, never smuggled in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import GlossaryEntry
from .text import has_han, sanitize_caption

_NEGATIONS = ("không", "chưa", "đừng", "chẳng", "chả")


@dataclass(slots=True)
class EditConfig:
    enabled: bool = True
    min_len_ratio: float = 0.75
    max_len_ratio: float = 1.12  # the editor must not blow the per-caption budget


def _digits(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


def gate_patch(current: str, patched: str, glossary: list[GlossaryEntry], config: EditConfig) -> str | None:
    """Return the accepted text, or None when the patch is not provably safe."""
    if not patched or has_han(patched):
        return None
    if current.count("⟂") != patched.count("⟂"):
        return None
    ratio = len(patched) / max(1, len(current))
    if ratio < config.min_len_ratio or ratio > config.max_len_ratio:
        return None
    if _digits(current) - _digits(patched):
        return None
    for negation in _NEGATIONS:
        if negation in current.lower() and negation not in patched.lower():
            return None
    for entry in glossary:
        term = entry.term_vi.strip()
        if term and term in current and term not in patched:
            return None
    return patched
