"""Gated semantic editor.

A review pass over the translated speech rows. The model returns FINDING/PATCH
rows; a PATCH is only applied when its anchor had a BLOCKER/ERROR finding in the
same response (severity gate), the patch is Han-free, and it does not collapse a
multi-segment anchor into too few cues (row-floor anti-collapse). Runs in rounds
until no patch is applied or a round cap is hit.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .glossary import format_glossary
from .llm import LLMConfig, complete, parse_tsv_rows
from .models import Anchor, GlossaryEntry, SpeechRow
from .prompts import EDITOR_SYSTEM, EDITOR_USER
from .text import clean, has_han, sanitize_caption, split_text_for_explicit_turns

_PATCH_SEVERITIES = {"BLOCKER", "ERROR"}


@dataclass(slots=True)
class EditorConfig:
    max_caption_chars: int = 32
    max_rounds: int = 2
    batch_anchors: int = 24
    workers: int = 4  # review batches in parallel (independent LLM calls)


def _group_by_anchor(rows: list[SpeechRow]) -> dict[int, list[SpeechRow]]:
    grouped: dict[int, list[SpeechRow]] = {}
    for row in rows:
        grouped.setdefault(row.anchor_id, []).append(row)
    return grouped


def _format_review_block(anchor: Anchor, rows: list[SpeechRow]) -> str:
    current = " ⟂ ".join(row.text for row in rows)
    return (
        f"<anchor id=\"{anchor.id}\" time=\"{anchor.start:.2f}-{anchor.end:.2f}\">\n"
        f"  SOURCE_ZH: {anchor.source_zh}\n"
        f"  CURRENT_VI: {current}\n"
        f"</anchor>"
    )


def _parse_findings_and_patches(raw: str) -> tuple[set[int], dict[int, str]]:
    flagged: set[int] = set()
    patches: dict[int, str] = {}
    for cells in parse_tsv_rows(raw, expected_cols=2):
        kind = cells[0].strip().upper()
        if kind == "FINDING":
            finding = parse_tsv_rows(f"{cells[0]}\t{cells[1]}", expected_cols=5)
            if finding:
                _, anchor_id, severity, _evidence, _issue = finding[0]
                if severity.strip().upper() in _PATCH_SEVERITIES and anchor_id.strip().isdigit():
                    flagged.add(int(anchor_id))
        elif kind == "PATCH":
            patch = parse_tsv_rows(f"{cells[0]}\t{cells[1]}", expected_cols=3)
            if patch:
                _, anchor_id, text = patch[0]
                if anchor_id.strip().isdigit() and sanitize_caption(text):
                    patches[int(anchor_id)] = sanitize_caption(text)
    return flagged, patches


def _row_floor(anchor: Anchor, current_rows: int) -> int:
    """Smallest acceptable cue count for an anchor — guards against collapse."""
    lo, hi = anchor.source_id_range()
    segments = max(1, hi - lo + 1)
    return min(current_rows, max(1, segments // 2))


def _apply_patch(anchor: Anchor, text: str, current_rows: list[SpeechRow], max_caption_chars: int, start_sub_id: int) -> list[SpeechRow] | None:
    if has_han(text):
        return None
    parts = split_text_for_explicit_turns(text, max_caption_chars) or [text]
    if len(parts) < _row_floor(anchor, len(current_rows)):
        return None  # anti-collapse: refuses to merge distinct turns away
    sub_id = start_sub_id
    rows: list[SpeechRow] = []
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


def _resequence(grouped: dict[int, list[SpeechRow]], anchors: list[Anchor]) -> list[SpeechRow]:
    out: list[SpeechRow] = []
    for anchor in anchors:
        for row in grouped.get(anchor.id, []):
            row.sub_id = len(out)
            out.append(row)
    return out


def edit_round(
    rows: list[SpeechRow], anchors: list[Anchor], glossary: list[GlossaryEntry], llm: LLMConfig, config: EditorConfig
) -> tuple[list[SpeechRow], int]:
    amap = {a.id: a for a in anchors}
    grouped = _group_by_anchor(rows)
    anchor_ids = [a.id for a in anchors if a.id in grouped]
    patched = 0
    glossary_text = format_glossary(glossary)

    def review(batch_ids: list[int]) -> tuple[set[int], dict[int, str]]:
        blocks = "\n".join(_format_review_block(amap[i], grouped[i]) for i in batch_ids)
        raw = complete(EDITOR_SYSTEM, EDITOR_USER.format(glossary=glossary_text, anchors=blocks), llm)
        return _parse_findings_and_patches(raw)

    batches = [anchor_ids[start : start + config.batch_anchors] for start in range(0, len(anchor_ids), config.batch_anchors)]
    # The review LLM calls are independent — run the batches concurrently, then
    # apply the gated patches in order.
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        results = list(pool.map(review, batches))

    for flagged, patches in results:
        for anchor_id, text in patches.items():
            if anchor_id not in flagged or anchor_id not in grouped:
                continue  # severity gate: no BLOCKER/ERROR finding -> ignore patch
            replacement = _apply_patch(amap[anchor_id], text, grouped[anchor_id], config.max_caption_chars, start_sub_id=0)
            if replacement is not None:
                grouped[anchor_id] = replacement
                patched += 1

    return _resequence(grouped, anchors), patched


def edit_speech(
    rows: list[SpeechRow],
    anchors: list[Anchor],
    glossary: list[GlossaryEntry],
    llm: LLMConfig,
    config: EditorConfig | None = None,
) -> tuple[list[SpeechRow], int]:
    config = config or EditorConfig()
    total_patched = 0
    for _ in range(max(1, config.max_rounds)):
        rows, patched = edit_round(rows, anchors, glossary, llm, config)
        total_patched += patched
        if patched == 0:
            break
    return rows, total_patched
