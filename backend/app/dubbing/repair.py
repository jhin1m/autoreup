"""Timing repair: shorten only the anchors whose speech overran their window.

Driven by measured render timings, not a guess. For each over-budget anchor we
compute a shorter character target (proportional to how far it overran), ask the
LLM to rewrite under that target while keeping key facts, and accept only if the
rewrite is genuinely shorter (by speech weight) and Han-free. Repair is local:
other anchors are untouched.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .glossary import format_glossary
from backend.app.agents import AgentConfig, complete

from .tsv_io import parse_tsv_rows
from .models import Anchor, GlossaryEntry, SpeechRow
from .prompts import EXPAND_SYSTEM, EXPAND_USER, REPAIR_SYSTEM, REPAIR_USER
from .text import has_han, sanitize_caption, split_text_for_explicit_turns_only
from .weights import vi_speech_weight

_MIN_TARGET_CHARS = 12


@dataclass(slots=True)
class RepairConfig:
    target_scale: float = 0.92  # shrink target a touch below the proportional estimate
    batch_anchors: int = 16
    batch_workers: int = 4  # batches are independent -> call the LLM concurrently


def _batched_rewrites(
    batches: list[str], system: str, llm: AgentConfig, workers: int
) -> dict[int, str]:
    """Run one LLM call per prompt batch CONCURRENTLY and merge the rewrites.

    A 30-min video's first repair round used to be ~9 serial LLM round-trips —
    the single biggest avoidable wait in the voiceover stage."""
    def call(user: str) -> dict[int, str]:
        raw = complete(system, user, llm)
        return {int(c[0]): sanitize_caption(c[1]) for c in parse_tsv_rows(raw, expected_cols=2) if c[0].strip().isdigit()}

    rewrites: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for result in pool.map(call, batches):
            rewrites.update(result)
    return rewrites


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
        # ≤N (same shape as the translate input) so an echoed budget is caught by
        # sanitize_caption — a bare number leaked into a caption verbatim before.
        lines.append(f"{target.anchor_id}\t≤{target.target_chars}\t{anchor.source_zh}\t{current}")
    return "\n".join(lines)


def _rebuild_rows(anchor: Anchor, text: str) -> list[SpeechRow]:
    # One row per unit (split only on explicit turns): caption sizing belongs to
    # the display reflow. TTS rejoins the rows per unit, so audio is unaffected.
    parts = split_text_for_explicit_turns_only(text) or [text]
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
    llm: AgentConfig,
    config: RepairConfig | None = None,
) -> tuple[list[SpeechRow], int]:
    config = config or RepairConfig()
    amap = {a.id: a for a in anchors}
    grouped = _group(speech_rows)
    targets = [t for t in compute_targets(grouped, overflow_by_anchor, config) if t.anchor_id in amap]
    if not targets:
        return speech_rows, 0

    batches = [
        REPAIR_USER.format(
            glossary=format_glossary(glossary),
            anchors=_format_repair_rows(targets[start : start + config.batch_anchors], amap, grouped),
        )
        for start in range(0, len(targets), config.batch_anchors)
    ]
    rewrites = _batched_rewrites(batches, REPAIR_SYSTEM, llm, config.batch_workers)

    repaired = 0
    for target in targets:
        text = rewrites.get(target.anchor_id)
        if not text or has_han(text):
            continue
        current_weight = sum(vi_speech_weight(r.text) for r in grouped[target.anchor_id])
        if vi_speech_weight(text) >= current_weight:
            continue  # not actually shorter -> reject
        grouped[target.anchor_id] = _rebuild_rows(amap[target.anchor_id], text)
        repaired += 1

    out: list[SpeechRow] = []
    for anchor in anchors:
        for row in grouped.get(anchor.id, []):
            row.sub_id = len(out)
            out.append(row)
    return out, repaired


def _format_expand_rows(anchor_ids: list[int], amap: dict[int, Anchor], grouped: dict[int, list[SpeechRow]], window: dict[int, float]) -> str:
    lines = []
    for anchor_id in anchor_ids:
        current = " ⟂ ".join(r.text for r in grouped[anchor_id])
        # ≈Ns so an echoed window duration is caught by sanitize_caption.
        lines.append(f"{anchor_id}\t≈{window[anchor_id]:.1f}s\t{amap[anchor_id].source_zh}\t{current}")
    return "\n".join(lines)


def expand(
    speech_rows: list[SpeechRow],
    anchors: list[Anchor],
    glossary: list[GlossaryEntry],
    underfull_by_anchor: dict[int, tuple[float, float]],
    llm: AgentConfig,
    config: RepairConfig | None = None,
) -> tuple[list[SpeechRow], int]:
    """Complete anchors whose voice is far shorter than the source window.

    ``underfull_by_anchor`` maps anchor_id -> (window_seconds, tts_duration). The
    voice being much shorter than the source usually means the translation dropped
    content; ask the LLM to restore the full source meaning (no invention). Accept
    only a meaningfully longer rewrite; if the LLM judges the line complete and
    keeps it, leave it unchanged.
    """
    config = config or RepairConfig()
    amap = {a.id: a for a in anchors}
    grouped = _group(speech_rows)
    ids = [aid for aid in underfull_by_anchor if aid in amap and aid in grouped]
    if not ids:
        return speech_rows, 0
    window = {aid: underfull_by_anchor[aid][0] for aid in ids}

    batches = [
        EXPAND_USER.format(
            glossary=format_glossary(glossary),
            anchors=_format_expand_rows(ids[start : start + config.batch_anchors], amap, grouped, window),
        )
        for start in range(0, len(ids), config.batch_anchors)
    ]
    rewrites = _batched_rewrites(batches, EXPAND_SYSTEM, llm, config.batch_workers)

    expanded = 0
    for anchor_id in ids:
        text = rewrites.get(anchor_id)
        if not text or has_han(text):
            continue
        current_weight = sum(vi_speech_weight(r.text) for r in grouped[anchor_id])
        if vi_speech_weight(text) <= current_weight * 1.1:
            continue  # not meaningfully longer -> keep current (line was complete)
        grouped[anchor_id] = _rebuild_rows(amap[anchor_id], text)
        expanded += 1

    out: list[SpeechRow] = []
    for anchor in anchors:
        for row in grouped.get(anchor.id, []):
            row.sub_id = len(out)
            out.append(row)
    return out, expanded
