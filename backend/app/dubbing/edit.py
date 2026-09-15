"""L4.5 — EDIT: one creative pass per batch, facts frozen by a code gate.

A real vietsub team has a translator and an editor. The translator's draft only has
to be faithful; the editor makes it *cuốn*. This stage is that editor: one pass per
batch, scene-level, then the words freeze — reflow and the timeline never rewrite.

The gate is what keeps the editor honest: a patch is accepted only when it cannot
have changed the meaning. Numbers, glossary terms, Vietnamese negations and turn
markers must survive; length must stay within +/-25%; no Han characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.app.agents import AgentConfig, complete

from .glossary import format_glossary
from .models import GlossaryEntry
from .prompts import EDIT_SYSTEM, EDIT_USER
from .text import has_han, sanitize_caption
from .tsv_io import parse_tsv_rows

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


def edit_texts(
    texts: dict[int, str],
    sources: dict[int, str],
    glossary: list[GlossaryEntry],
    llm: AgentConfig,
    config: EditConfig | None = None,
) -> tuple[dict[int, str], int]:
    """One editor pass over a batch; returns (texts, accepted_patch_count).

    ``texts``/``sources`` are keyed by caption id (one line each)."""
    config = config or EditConfig()
    if not config.enabled or not texts:
        return texts, 0
    rows = "\n".join(f"{cid}\t{sources.get(cid, '')}\t{texts[cid]}" for cid in texts)
    if not rows:
        return texts, 0
    try:
        raw = complete(EDIT_SYSTEM, EDIT_USER.format(glossary=format_glossary(glossary), rows=rows), llm)
    except Exception:  # noqa: BLE001 - the editor is best-effort; never fail the job
        return texts, 0

    valid_ids = set(texts)
    patched: dict[int, str] = {}
    for cells in parse_tsv_rows(raw, expected_cols=2):
        try:
            caption_id = int(cells[0].strip().lstrip("#"))
        except ValueError:
            continue
        if caption_id in valid_ids:
            text = sanitize_caption(cells[1])
            if text:
                patched[caption_id] = text

    accepted = 0
    result: dict[int, str] = {}
    for caption_id, current in texts.items():
        candidate = patched.get(caption_id)
        if candidate is None:
            result[caption_id] = current
            continue
        gated = gate_patch(current, candidate, glossary, config)
        if gated is None:
            result[caption_id] = current
        else:
            result[caption_id] = gated
            accepted += 1 if gated != current else 0
    return result, accepted
