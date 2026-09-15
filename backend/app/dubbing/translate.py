"""L4 — unit translation (F1: meaning lives at the sentence, never at display fragments).

A caption is a *display* fragment; a unit is a complete thought. Translating per
caption (with a per-caption time budget) forces condensed fragments and measurably
damages the writing, so the translator always sees the whole unit and returns ONE
natural Vietnamese line for it. Timing is never budgeted in the prompt — the render
measures the real TTS durations and the fit loop closes the gap with facts.

Pipeline: batch units (rolling context) -> gated EDIT pass -> one row per unit.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.agents import AgentConfig, complete

from .edit import EditConfig, edit_texts
from .glossary import format_glossary
from .models import Anchor, GlossaryEntry, SpeechRow
from .prompts import TRANSLATE_SYSTEM, TRANSLATE_USER
from .text import has_han, has_kana, sanitize_caption
from .tsv_io import parse_tsv_rows

_CONTEXT_SOURCE_CHARS = 200
_CONTEXT_VI_CHARS = 240


class TranslateError(RuntimeError):
    pass


@dataclass(slots=True)
class TranslateConfig:
    notes: str = ""           # per-video context (cast/gender/register) injected into the prompt
    batch_units: int = 20     # units per translation call
    edit: bool = True         # one gated creative pass per batch (L4.5)


def _parse_output(raw: str, valid_ids: set[int]) -> dict[int, str]:
    texts: dict[int, str] = {}
    for cells in parse_tsv_rows(raw, expected_cols=2):
        try:
            unit_id = int(cells[0].strip().lstrip("#"))
        except ValueError:
            continue
        if unit_id not in valid_ids:
            continue
        text = sanitize_caption(cells[1])
        if text:
            texts[unit_id] = text
    return texts


def _bad_ids(texts: dict[int, str], units: list[Anchor]) -> list[int]:
    return [a.id for a in units if not texts.get(a.id) or has_han(texts[a.id]) or has_kana(texts[a.id])]


def translate_batch(
    units: list[Anchor],
    glossary: list[GlossaryEntry],
    prev_source: str,
    prev_vi: str,
    llm: AgentConfig,
    config: TranslateConfig,
) -> dict[int, str]:
    def attempt(batch: list[Anchor]) -> dict[int, str]:
        user = TRANSLATE_USER.format(
            glossary=format_glossary(glossary),
            notes=config.notes or "(không có)",
            prev_source=prev_source or "(đầu video)",
            prev_vi=prev_vi or "(chưa có)",
            units="\n".join(f"{a.id}\t{a.source_zh}" for a in batch),
        )
        return _parse_output(complete(TRANSLATE_SYSTEM, user, llm), {a.id for a in batch})

    texts = attempt(units)
    # per-line flake retry (skipped line / Han leak), not a whole-batch failure
    bad = _bad_ids(texts, units)
    if bad:
        retried = attempt([a for a in units if a.id in set(bad)])
        texts.update({k: v for k, v in retried.items() if k in set(bad) and v and not has_han(v)})
    missing = _bad_ids(texts, units)
    if missing:
        raise TranslateError(f"units without a translation: {missing[:10]}")

    if config.edit:
        texts, _accepted = edit_texts(texts, {a.id: a.source_zh for a in units}, glossary, llm)
        missing = _bad_ids(texts, units)
        if missing:
            raise TranslateError(f"edit dropped units: {missing[:10]}")
    return texts


def translate(
    units: list[Anchor],
    glossary: list[GlossaryEntry],
    llm: AgentConfig,
    config: TranslateConfig | None = None,
) -> list[SpeechRow]:
    """One Vietnamese row per semantic unit, on the unit's own window."""
    config = config or TranslateConfig()
    if not units:
        return []

    texts: dict[int, str] = {}
    prev_source = ""
    prev_vi = ""
    for start in range(0, len(units), max(1, config.batch_units)):
        batch = units[start : start + config.batch_units]
        batch_texts = translate_batch(batch, glossary, prev_source, prev_vi, llm, config)
        texts.update(batch_texts)
        prev_source = "".join(a.source_zh for a in batch)[-_CONTEXT_SOURCE_CHARS:]
        prev_vi = " ".join(batch_texts[a.id] for a in batch if batch_texts.get(a.id))[-_CONTEXT_VI_CHARS:]

    return [
        SpeechRow(
            sub_id=index,
            anchor_id=unit.id,
            anchor_start=unit.start,
            anchor_end=unit.end,
            source_ids=unit.source_ids,
            text=texts[unit.id],
        )
        for index, unit in enumerate(units)
        if texts.get(unit.id)
    ]
