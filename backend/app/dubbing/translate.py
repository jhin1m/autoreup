"""Stateful chunk translation: Chinese anchors -> Vietnamese speech rows.

Each chunk is translated with rolling MEMORY and the previous dub tail carried
forward, so names and plot stay consistent across a long video without one giant
context. The model emits ``ANCHOR_ID<TAB>TEXT`` rows (plus optional ``MEMORY``
rows); we validate (no Han, anchors in range, full coverage) and split each
anchor's text into cue-sized SpeechRows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .chunking import anchors_for_chunk
from .glossary import format_glossary
from .llm import LLMConfig, complete, parse_tsv_rows
from .models import Anchor, Chunk, GlossaryEntry, SpeechRow
from .prompts import TRANSLATE_SYSTEM, TRANSLATE_USER
from .text import clean, has_han, split_text_for_explicit_turns

_TAIL_CHARS = 240
_MEMORY_LIMIT = 40


class TranslateError(RuntimeError):
    pass


@dataclass(slots=True)
class TranslateConfig:
    max_caption_chars: int = 32
    context_anchors: int = 3  # anchors of trailing context shown as the dub tail


@dataclass(slots=True)
class _State:
    memory: dict[str, str] = field(default_factory=dict)
    tail: str = ""


def _format_anchor_rows(anchors: list[Anchor]) -> str:
    return "\n".join(
        f"{a.id}\t{a.start:.2f}-{a.end:.2f}\t{a.source_zh}" for a in anchors
    )


def _format_memory(memory: dict[str, str]) -> str:
    if not memory:
        return "(trống)"
    return "\n".join(f"{key}: {value}" for key, value in memory.items())


def _parse_chunk_output(raw: str, valid_ids: set[int]) -> tuple[dict[int, str], dict[str, str]]:
    texts: dict[int, str] = {}
    memory: dict[str, str] = {}
    for cells in parse_tsv_rows(raw, expected_cols=2):
        head = cells[0].strip()
        if head.upper() == "MEMORY":
            mem_cells = parse_tsv_rows(f"{cells[0]}\t{cells[1]}", expected_cols=3)
            if mem_cells:
                _, key, value = mem_cells[0]
                if clean(key):
                    memory[clean(key)] = clean(value)
            continue
        try:
            anchor_id = int(head)
        except ValueError:
            continue
        if anchor_id not in valid_ids:
            continue
        text = clean(cells[1])
        if text:
            texts[anchor_id] = text
    return texts, memory


def _validate(texts: dict[int, str], anchors: list[Anchor]) -> None:
    for anchor in anchors:
        text = texts.get(anchor.id)
        if not text:
            raise TranslateError(f"anchor {anchor.id} has no translation")
        if has_han(text):
            raise TranslateError(f"anchor {anchor.id} translation still contains Han characters")


def _speech_rows_from_texts(
    texts: dict[int, str], anchors: list[Anchor], max_caption_chars: int, start_sub_id: int
) -> list[SpeechRow]:
    rows: list[SpeechRow] = []
    sub_id = start_sub_id
    for anchor in anchors:
        parts = split_text_for_explicit_turns(texts[anchor.id], max_caption_chars) or [texts[anchor.id]]
        for part in parts:
            rows.append(
                SpeechRow(
                    sub_id=sub_id,
                    anchor_id=anchor.id,
                    anchor_start=anchor.start,
                    anchor_end=anchor.end,
                    source_ids=anchor.source_ids,
                    text=part,
                )
            )
            sub_id += 1
    return rows


def translate_chunk(
    chunk_anchors: list[Anchor],
    glossary: list[GlossaryEntry],
    state: _State,
    llm: LLMConfig,
    config: TranslateConfig,
    start_sub_id: int,
) -> list[SpeechRow]:
    valid_ids = {a.id for a in chunk_anchors}
    user = TRANSLATE_USER.format(
        glossary=format_glossary(glossary),
        memory=_format_memory(state.memory),
        prev_tail=state.tail or "(đầu video)",
        anchors=_format_anchor_rows(chunk_anchors),
        max_caption_chars=config.max_caption_chars,
    )
    raw = complete(TRANSLATE_SYSTEM, user, llm)
    texts, memory = _parse_chunk_output(raw, valid_ids)
    _validate(texts, chunk_anchors)

    for key, value in memory.items():
        state.memory[key] = value
    if len(state.memory) > _MEMORY_LIMIT:
        state.memory = dict(list(state.memory.items())[-_MEMORY_LIMIT:])

    rows = _speech_rows_from_texts(texts, chunk_anchors, config.max_caption_chars, start_sub_id)
    tail_anchor_ids = [a.id for a in chunk_anchors[-config.context_anchors :]]
    state.tail = clean(" ".join(texts[i] for i in tail_anchor_ids))[-_TAIL_CHARS:]
    return rows


def translate(
    anchors: list[Anchor],
    chunks: list[Chunk],
    glossary: list[GlossaryEntry],
    llm: LLMConfig,
    config: TranslateConfig | None = None,
) -> list[SpeechRow]:
    config = config or TranslateConfig()
    state = _State()
    rows: list[SpeechRow] = []
    for chunk in chunks:
        chunk_anchors = anchors_for_chunk(chunk, anchors)
        if not chunk_anchors:
            continue
        rows.extend(translate_chunk(chunk_anchors, glossary, state, llm, config, start_sub_id=len(rows)))
    return rows
