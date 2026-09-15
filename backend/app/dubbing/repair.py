"""Timing fit: shorten lines that overrun the free time, complete lines that fall
far short of the source speech (measured, per row).

Every row carries a real caption window and a measured TTS duration, so the budget
is a fact, not a guess: the free time is the gap until the next caption's picture,
and the underfill budget is the VAD speech the row must cover. Rewrites are local
(one row), accepted only when genuinely shorter/longer by speech weight and Han-free.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .glossary import format_glossary
from backend.app.agents import AgentConfig, complete

from .tsv_io import parse_tsv_rows
from .models import GlossaryEntry, SpeechRow
from .prompts import EXPAND_SYSTEM, EXPAND_USER, REPAIR_SYSTEM, REPAIR_USER
from .text import has_han, sanitize_caption
from .weights import vi_speech_weight

_MIN_TARGET_CHARS = 8


@dataclass(slots=True)
class RepairConfig:
    target_scale: float = 0.92  # shrink target a touch below the proportional estimate
    batch_rows: int = 16
    batch_workers: int = 4  # batches are independent -> call the LLM concurrently


def _batched_rewrites(
    batches: list[str], system: str, llm: AgentConfig, workers: int
) -> dict[int, str]:
    """Run one LLM call per prompt batch CONCURRENTLY and merge the rewrites."""
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
    sub_id: int
    target_chars: int


def compute_targets(
    rows: dict[int, SpeechRow],
    overflow: dict[int, tuple[float, float]],
    config: RepairConfig,
) -> list[RepairTarget]:
    """``overflow`` maps sub_id -> (free_seconds, measured_seconds)."""
    targets: list[RepairTarget] = []
    for sub_id, (free_seconds, measured) in overflow.items():
        row = rows.get(sub_id)
        if row is None or measured <= 0:
            continue
        ratio = max(0.3, min(1.0, free_seconds / measured)) * config.target_scale
        target_chars = max(_MIN_TARGET_CHARS, int(len(row.text) * ratio))
        targets.append(RepairTarget(sub_id=sub_id, target_chars=target_chars))
    return targets


def _format_rows(targets: list[RepairTarget], rows: dict[int, SpeechRow], sources: dict[int, str], budget: dict[int, float], measured: dict[int, float]) -> str:
    lines = []
    for target in targets:
        row = rows[target.sub_id]
        # seconds columns only as context; an echoed number is caught by sanitize_caption
        lines.append(
            f"{target.sub_id}\t{measured[target.sub_id]:.1f}s\t{budget[target.sub_id]:.1f}s\t"
            f"{sources.get(target.sub_id, '')}\t{row.text}"
        )
    return "\n".join(lines)


def _apply_rewrites(
    speech_rows: list[SpeechRow],
    candidates: dict[int, str],
    accept,
) -> tuple[list[SpeechRow], int]:
    """Return a NEW row list with the accepted rewrites; never mutates the input."""
    changed = 0
    out: list[SpeechRow] = []
    for row in speech_rows:
        text = candidates.get(row.sub_id)
        if text and not has_han(text) and accept(row, text):
            out.append(SpeechRow(row.sub_id, row.anchor_id, row.anchor_start, row.anchor_end, row.source_ids, text))
            changed += 1
        else:
            out.append(SpeechRow(row.sub_id, row.anchor_id, row.anchor_start, row.anchor_end, row.source_ids, row.text))
    return out, changed


def repair(
    speech_rows: list[SpeechRow],
    sources: dict[int, str],
    glossary: list[GlossaryEntry],
    overflow: dict[int, tuple[float, float]],
    llm: AgentConfig,
    config: RepairConfig | None = None,
) -> tuple[list[SpeechRow], int]:
    config = config or RepairConfig()
    by_id = {row.sub_id: row for row in speech_rows}
    targets = compute_targets(by_id, overflow, config)
    if not targets:
        return speech_rows, 0

    budget = {t.sub_id: overflow[t.sub_id][0] for t in targets}
    measured = {t.sub_id: overflow[t.sub_id][1] for t in targets}
    batches = [
        REPAIR_USER.format(
            glossary=format_glossary(glossary),
            rows=_format_rows(targets[start : start + config.batch_rows], by_id, sources, budget, measured),
        )
        for start in range(0, len(targets), config.batch_rows)
    ]
    rewrites = _batched_rewrites(batches, REPAIR_SYSTEM, llm, config.batch_workers)

    return _apply_rewrites(
        speech_rows,
        rewrites,
        lambda row, text: vi_speech_weight(text) < vi_speech_weight(row.text),  # must be shorter
    )


def expand(
    speech_rows: list[SpeechRow],
    sources: dict[int, str],
    glossary: list[GlossaryEntry],
    underfull: dict[int, tuple[float, float]],
    llm: AgentConfig,
    config: RepairConfig | None = None,
) -> tuple[list[SpeechRow], int]:
    """Complete rows whose voice is far shorter than the source speech.

    ``underfull`` maps sub_id -> (source_speech_seconds, measured_seconds). The LLM
    may keep a line it judges complete; only a meaningfully longer rewrite is accepted.
    """
    config = config or RepairConfig()
    by_id = {row.sub_id: row for row in speech_rows}
    ids = [sub_id for sub_id in underfull if sub_id in by_id]
    if not ids:
        return speech_rows, 0
    speech = {sub_id: underfull[sub_id][0] for sub_id in ids}
    measured = {sub_id: underfull[sub_id][1] for sub_id in ids}

    # reuse the repair formatter with the speech budget in the "budget" column
    targets = [RepairTarget(sub_id=sub_id, target_chars=len(by_id[sub_id].text)) for sub_id in ids]
    batches = [
        EXPAND_USER.format(
            glossary=format_glossary(glossary),
            rows=_format_rows(targets[start : start + config.batch_rows], by_id, sources, speech, measured),
        )
        for start in range(0, len(ids), config.batch_rows)
    ]
    rewrites = _batched_rewrites(batches, EXPAND_SYSTEM, llm, config.batch_workers)

    return _apply_rewrites(
        speech_rows,
        {sub_id: rewrites[sub_id] for sub_id in ids if sub_id in rewrites},
        lambda row, text: vi_speech_weight(text) > vi_speech_weight(row.text) * 1.1,  # meaningfully longer
    )
