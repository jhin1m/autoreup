"""L4 — translate caption by caption (v3): the caption IS the timing unit.

A source caption is authored against the source voice: it appears when its line is
spoken. Translating (and later voicing/displaying) per caption makes every row's
window exact by construction — no char-proportional interpolation inside a merged
unit, no run packing, no clamps. Units still group captions for translation
*context* (the prompt shows the unit's captions together so the sentence reads as
one), but the output is one Vietnamese line per caption id.

Pipeline: batch units (rolling context) -> gated EDIT pass -> one row per caption.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.agents import AgentConfig, complete

from .edit import EditConfig, edit_texts
from .glossary import format_glossary
from .models import Anchor, AsrSegment, GlossaryEntry, SpeechRow
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
    voice_cps: float = 19.8   # MEASURED natural read pace of the voice (chars/second)


def _units_block(units: list[Anchor], captions: dict[int, list[AsrSegment]], voice_cps: float) -> str:
    """Caption rows carry their OWN reading budget: the rail's caption duration.

    Writing to length is the only correct fix for a dub that is longer than the time
    it has (per-caption anchoring cannot compress; measured: 73% of rows ran over
    their window at natural pace). The fit loop then verifies with MEASURED TTS
    durations, so this hint can be a little off without breaking anything."""
    blocks = []
    for unit in units:
        lines = []
        for seg in captions.get(unit.id, []):
            seconds = max(0.4, seg.end - seg.start)
            lines.append(f"{seg.id}\t~{seconds:.1f}s (~{round(seconds * voice_cps)} ký tự)\t{seg.text_zh}")
        blocks.append(f"# {unit.id}\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def _parse_output(raw: str, valid_ids: set[int]) -> dict[int, str]:
    texts: dict[int, str] = {}
    for cells in parse_tsv_rows(raw, expected_cols=2):
        try:
            caption_id = int(cells[0].strip().lstrip("#"))
        except ValueError:
            continue
        if caption_id not in valid_ids:
            continue
        text = sanitize_caption(cells[1])
        if text:
            texts[caption_id] = text
    return texts


def _bad_ids(texts: dict[int, str], ids: list[int]) -> list[int]:
    return [i for i in ids if not texts.get(i) or has_han(texts[i]) or has_kana(texts[i])]


def translate_batch(
    units: list[Anchor],
    captions: dict[int, list[AsrSegment]],
    glossary: list[GlossaryEntry],
    prev_source: str,
    prev_vi: str,
    llm: AgentConfig,
    config: TranslateConfig,
) -> dict[int, str]:
    ids = [seg.id for unit in units for seg in captions.get(unit.id, [])]
    sources = {seg.id: seg.text_zh for unit in units for seg in captions.get(unit.id, [])}

    def attempt(batch: list[Anchor]) -> dict[int, str]:
        user = TRANSLATE_USER.format(
            glossary=format_glossary(glossary),
            notes=config.notes or "(không có)",
            prev_source=prev_source or "(đầu video)",
            prev_vi=prev_vi or "(chưa có)",
            units=_units_block(batch, captions, config.voice_cps),
        )
        return _parse_output(complete(TRANSLATE_SYSTEM, user, llm), set(sources))

    texts = attempt(units)
    bad = _bad_ids(texts, ids)
    if bad:
        # per-line flake retry (skipped line / Han leak), not a whole-batch failure
        retry_units = [u for u in units if any(i in set(bad) for i in (seg.id for seg in captions.get(u.id, [])))]
        texts.update({k: v for k, v in attempt(retry_units).items() if k in set(bad) and v and not has_han(v)})
    missing = _bad_ids(texts, ids)
    if missing:
        raise TranslateError(f"captions without a translation: {missing[:10]}")

    if config.edit:
        texts, _accepted = edit_texts(texts, sources, glossary, llm)
        missing = _bad_ids(texts, ids)
        if missing:
            raise TranslateError(f"edit dropped captions: {missing[:10]}")
    return texts


def translate(
    units: list[Anchor],
    segments: list[AsrSegment],
    glossary: list[GlossaryEntry],
    llm: AgentConfig,
    config: TranslateConfig | None = None,
) -> list[SpeechRow]:
    """One Vietnamese row per source caption, timed to that caption's window."""
    config = config or TranslateConfig()
    if not units:
        return []
    seg_by_id = {seg.id: seg for seg in segments}
    captions: dict[int, list[AsrSegment]] = {}
    for unit in units:
        lo, _, hi = unit.source_ids.partition("-")
        captions[unit.id] = [seg_by_id[i] for i in range(int(lo), int(hi) + 1) if i in seg_by_id]

    texts: dict[int, str] = {}
    prev_source = ""
    prev_vi = ""
    for start in range(0, len(units), max(1, config.batch_units)):
        batch = units[start : start + config.batch_units]
        batch_texts = translate_batch(batch, captions, glossary, prev_source, prev_vi, llm, config)
        texts.update(batch_texts)
        prev_source = "".join(seg.text_zh for u in batch for seg in captions.get(u.id, []))[-_CONTEXT_SOURCE_CHARS:]
        prev_vi = " ".join(batch_texts[seg.id] for u in batch for seg in captions.get(u.id, []) if batch_texts.get(seg.id))[
            -_CONTEXT_VI_CHARS:
        ]

    rows: list[SpeechRow] = []
    for unit in units:
        for seg in captions.get(unit.id, []):
            text = texts.get(seg.id)
            if not text:
                continue
            rows.append(
                SpeechRow(
                    sub_id=len(rows),
                    anchor_id=unit.id,
                    anchor_start=seg.start,
                    anchor_end=seg.end,
                    source_ids=f"{seg.id}-{seg.id}",
                    text=text,
                )
            )
    return rows
