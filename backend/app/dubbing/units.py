"""L2 — semantic units: the translation unit of the v3 pipeline (deterministic).

A unit is one sentence-ish piece of the source (the thing a translator would work
on as a whole), built by merging ASR micro-segments on punctuation, pause and
length. Units keep their ``segment_ids`` so timing still belongs to the rail —
units are a meaning concept, never a timing source.

Rationale (measured): translating per anchor and splitting captions up front
produced 2.62 rows/anchor with 28.4% of rows ending mid-sentence. Translating a
complete sentence and letting L5 reflow cut display lines fixes the shape.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import AsrSegment, Unit

SENTENCE_ENDINGS = "。！？!?…；;"


@dataclass(slots=True)
class UnitConfig:
    max_chars: int = 60       # normal sentence size — cut once a unit reaches this
    hard_max_chars: int = 90  # never let a unit grow past this
    min_chars: int = 6        # fragments below this merge forward
    gap_cut: float = 0.45     # a pause this long ends the unit (no punctuation in ASR)
    merge_gap: float = 0.6    # ...but only merge a small fragment across a shorter gap


def build_units(segments: list[AsrSegment], config: UnitConfig | None = None) -> list[Unit]:
    config = config or UnitConfig()
    units: list[Unit] = []
    current: list[AsrSegment] = []

    def flush() -> None:
        if not current:
            return
        units.append(
            Unit(
                id=len(units),
                segment_ids=f"{current[0].id}-{current[-1].id}",
                start=current[0].start,
                end=current[-1].end,
                source_zh="".join(s.text_zh for s in current).strip(),
            )
        )
        current.clear()

    for segment in segments:
        if current:
            previous = current[-1]
            gap = segment.start - previous.end
            length = sum(len(s.text_zh) for s in current)
            ends_sentence = previous.text_zh.rstrip().endswith(tuple(SENTENCE_ENDINGS))
            if (
                gap >= config.gap_cut
                or ends_sentence
                or length >= config.max_chars
                or length + len(segment.text_zh) > config.hard_max_chars
            ):
                flush()
        current.append(segment)
    flush()

    # Merge tiny leading fragments forward so the translator never gets a stub line —
    # but never merge across a deliberate sentence boundary (punctuation wins).
    merged: list[Unit] = []
    for unit in units:
        previous = merged[-1] if merged else None
        fragment = previous is not None and len(previous.source_zh) < config.min_chars
        deliberate = previous is not None and previous.source_zh.rstrip().endswith(tuple(SENTENCE_ENDINGS))
        if previous is not None and fragment and not deliberate and unit.start - previous.end < config.merge_gap:
            merged[-1] = Unit(
                id=previous.id,
                segment_ids=previous.segment_ids,
                start=previous.start,
                end=unit.end,
                source_zh=previous.source_zh + unit.source_zh,
                flags=previous.flags,
            )
        else:
            merged.append(replace(unit, id=len(merged)))
    return merged
