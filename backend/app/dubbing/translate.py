"""Chunk translation: Chinese anchors -> Vietnamese speech rows.

Chunks are independent and translated CONCURRENTLY: consistency across a long
video comes from the glossary (names, gender, fixed pronouns) plus a slice of
the previous chunk's SOURCE text for local continuity — not from a rolling
translated-state chain, which forced chunks to run one-by-one and made a long
video's translate time scale linearly. The model emits ``ANCHOR_ID<TAB>TEXT``
rows; we validate (no Han, anchors in range, full coverage) and split each
anchor's text into cue-sized SpeechRows.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .chunking import anchors_for_chunk
from .glossary import format_glossary
from .llm import LLMConfig, complete, parse_tsv_rows
from .models import Anchor, Chunk, GlossaryEntry, SpeechRow
from .prompts import TRANSLATE_SYSTEM, TRANSLATE_USER
from .text import has_han, sanitize_caption, split_text_for_explicit_turns

_CONTEXT_SOURCE_CHARS = 160


class TranslateError(RuntimeError):
    pass


@dataclass(slots=True)
class TranslateConfig:
    max_caption_chars: int = 32
    voice_cps: float = 13.0   # per-anchor char budget = duration * voice_cps (natural read pace)
    min_budget_chars: int = 12
    notes: str = ""           # per-video context (cast/gender/register) injected into the prompt
    chunk_workers: int = 3    # concurrent chunk translations (bounded: the LLM proxy throttles)


def _budget_chars(anchor: Anchor, voice_cps: float, floor: int) -> int:
    return max(floor, int(round((anchor.end - anchor.start) * voice_cps)))


def _format_anchor_rows(anchors: list[Anchor], voice_cps: float, floor: int) -> str:
    # Each row carries an explicit character budget = how much Vietnamese fits in the
    # time this line is spoken. The model must condense to it (meaning over verbatim).
    # No raw start-end timestamp column: the prompt never uses it, and having it inline
    # made the model occasionally echo "8.03-14.39 ≤114 ..." into the translated TEXT.
    return "\n".join(
        f"{a.id}\t≤{_budget_chars(a, voice_cps, floor)}\t{a.source_zh}"
        for a in anchors
    )


def _parse_chunk_output(raw: str, valid_ids: set[int]) -> dict[int, str]:
    texts: dict[int, str] = {}
    for cells in parse_tsv_rows(raw, expected_cols=2):
        head = cells[0].strip()
        if head.upper() == "MEMORY":
            continue  # tolerated legacy row shape; state is no longer threaded
        try:
            anchor_id = int(head)
        except ValueError:
            continue
        if anchor_id not in valid_ids:
            continue
        text = sanitize_caption(cells[1])
        if text:
            texts[anchor_id] = text
    return texts


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
    prev_source: str,
    llm: LLMConfig,
    config: TranslateConfig,
) -> dict[int, str]:
    def attempt(anchors: list[Anchor]) -> dict[int, str]:
        user = TRANSLATE_USER.format(
            glossary=format_glossary(glossary),
            notes=config.notes or "(không có)",
            prev_source=prev_source or "(đầu video)",
            anchors=_format_anchor_rows(anchors, config.voice_cps, config.min_budget_chars),
            max_caption_chars=config.max_caption_chars,
        )
        raw = complete(TRANSLATE_SYSTEM.format(max_caption_chars=config.max_caption_chars), user, llm)
        return _parse_chunk_output(raw, {a.id for a in anchors})

    texts = attempt(chunk_anchors)
    # Content-level retry: the model sometimes skips an anchor or leaves Han
    # characters in one line. That is a per-line flake, not a chunk problem — re-ask
    # for JUST the bad anchors once instead of failing the whole job
    # ("anchor 15 translation still contains Han characters").
    bad = [a for a in chunk_anchors if not texts.get(a.id) or has_han(texts[a.id])]
    if bad:
        retried = attempt(bad)
        texts.update({aid: t for aid, t in retried.items() if t and not has_han(t)})
    _validate(texts, chunk_anchors)
    return texts


def translate(
    anchors: list[Anchor],
    chunks: list[Chunk],
    glossary: list[GlossaryEntry],
    llm: LLMConfig,
    config: TranslateConfig | None = None,
) -> list[SpeechRow]:
    config = config or TranslateConfig()
    chunk_anchor_lists = [anchors_for_chunk(chunk, anchors) for chunk in chunks]
    jobs = [(i, ca) for i, ca in enumerate(chunk_anchor_lists) if ca]
    if not jobs:
        return []

    def prev_source_for(index: int) -> str:
        if index == 0:
            return ""
        prev = chunk_anchor_lists[index - 1]
        return "".join(a.source_zh for a in prev)[-_CONTEXT_SOURCE_CHARS:]

    with ThreadPoolExecutor(max_workers=max(1, config.chunk_workers)) as pool:
        texts_per_chunk = list(pool.map(
            lambda job: translate_chunk(job[1], glossary, prev_source_for(job[0]), llm, config),
            jobs,
        ))

    rows: list[SpeechRow] = []
    for (_, chunk_anchors), texts in zip(jobs, texts_per_chunk):
        rows.extend(_speech_rows_from_texts(texts, chunk_anchors, config.max_caption_chars, start_sub_id=len(rows)))
    return rows
