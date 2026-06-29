"""Timing repair: shorten only the anchors whose speech overran their window.

Driven by measured render timings, not a guess. For each over-budget anchor we
compute a shorter character target (proportional to how far it overran), ask the
LLM to rewrite under that target while keeping key facts, and accept only if the
rewrite is genuinely shorter (by speech weight) and Han-free. Repair is local:
other anchors are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from .glossary import format_glossary
from .llm import LLMConfig, complete, parse_tsv_rows
from .models import Anchor, GlossaryEntry, SpeechRow
from .prompts import REPAIR_SYSTEM, REPAIR_USER
from .text import clean, has_han, split_text_for_explicit_turns
from .weights import vi_speech_weight

_MIN_TARGET_CHARS = 12


@dataclass(slots=True)
class RepairConfig:
    max_caption_chars: int = 32
    target_scale: float = 0.92  # shrink target a touch below the proportional estimate
    batch_anchors: int = 16


@dataclass(slots=True)
class RepairTarget:
    anchor_id: int
    target_chars: int


def _group(rows: list[SpeechRow]) -> dict[int, list[SpeechRow]]:
    grouped: dict[int, list[SpeechRow]] = {}
    for row in rows:
        grouped.setdefault(row.anchor_id, []).append(row)
    return grouped


def compute_targets(
    grouped: dict[int, list[SpeechRow]],
    overflow_by_anchor: dict[int, tuple[float, float]],
    config: RepairConfig,
) -> list[RepairTarget]:
    """``overflow_by_anchor`` maps anchor_id -> (target_duration, tts_duration)."""
    targets: list[RepairTarget] = []
    for anchor_id, (target_duration, tts_duration) in overflow_by_anchor.items():
        rows = grouped.get(anchor_id)
        if not rows or tts_duration <= 0:
            continue
        current_chars = sum(len(r.text) for r in rows)
        ratio = max(0.3, min(1.0, target_duration / tts_duration)) * config.target_scale
        target_chars = max(_MIN_TARGET_CHARS, int(current_chars * ratio))
        targets.append(RepairTarget(anchor_id=anchor_id, target_chars=target_chars))
    return targets


def _format_repair_rows(targets: list[RepairTarget], amap: dict[int, Anchor], grouped: dict[int, list[SpeechRow]]) -> str:
    lines = []
    for target in targets:
        anchor = amap[target.anchor_id]
        current = " ⟂ ".join(r.text for r in grouped[target.anchor_id])
        lines.append(f"{target.anchor_id}\t{target.target_chars}\t{anchor.source_zh}\t{current}")
    return "\n".join(lines)


def _rebuild_rows(anchor: Anchor, text: str, max_caption_chars: int) -> list[SpeechRow]:
    parts = split_text_for_explicit_turns(text, max_caption_chars) or [text]
    return [
        SpeechRow(
            sub_id=0,
            anchor_id=anchor.id,
            anchor_start=anchor.start,
            anchor_end=anchor.end,
            source_ids=anchor.source_ids,
            text=part,
        )
        for part in parts
    ]


def repair(
    speech_rows: list[SpeechRow],
    anchors: list[Anchor],
    glossary: list[GlossaryEntry],
    overflow_by_anchor: dict[int, tuple[float, float]],
    llm: LLMConfig,
    config: RepairConfig | None = None,
) -> tuple[list[SpeechRow], int]:
    config = config or RepairConfig()
    amap = {a.id: a for a in anchors}
    grouped = _group(speech_rows)
    targets = [t for t in compute_targets(grouped, overflow_by_anchor, config) if t.anchor_id in amap]
    if not targets:
        return speech_rows, 0

    repaired = 0
    for start in range(0, len(targets), config.batch_anchors):
        batch = targets[start : start + config.batch_anchors]
        user = REPAIR_USER.format(glossary=format_glossary(glossary), anchors=_format_repair_rows(batch, amap, grouped))
        raw = complete(REPAIR_SYSTEM, user, llm)
        rewrites = {int(c[0]): clean(c[1]) for c in parse_tsv_rows(raw, expected_cols=2) if c[0].strip().isdigit()}
        for target in batch:
            text = rewrites.get(target.anchor_id)
            if not text or has_han(text):
                continue
            current_weight = sum(vi_speech_weight(r.text) for r in grouped[target.anchor_id])
            if vi_speech_weight(text) >= current_weight:
                continue  # not actually shorter -> reject
            grouped[target.anchor_id] = _rebuild_rows(amap[target.anchor_id], text, config.max_caption_chars)
            repaired += 1

    out: list[SpeechRow] = []
    for anchor in anchors:
        for row in grouped.get(anchor.id, []):
            row.sub_id = len(out)
            out.append(row)
    return out, repaired
