"""Turn speech rows into one-line display subtitle cues.

The Vietnamese subtitle is re-segmented to follow the Chinese ASR rail: the
anchor's Vietnamese text is split at natural clause boundaries (commas, full
stops…) and each clause is timed to where its content is actually spoken,
interpolated across the source ASR segments (honouring the gaps between them).
This keeps the Vietnamese subtitle as finely separated as the Chinese source —
without breaking words mid-phrase — and stops cues running ahead of the original
voice. Re-segmentation is display-only: it never re-translates and never changes
the per-anchor voice-over. Falls back to a proportional spread when segment
timing isn't available.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.app.services.subtitle_formats import write_srt, write_vtt
from .models import Anchor, AsrSegment, SpeechRow
from .text import strip_explicit_turn_markers
from .tsv_io import write_json

_MIN_CUE_CHARS = 6
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,.!?;…，。！？；])\s+")


def _wrap_long(clause: str, max_chars: int) -> list[str]:
    if len(clause) <= max_chars:
        return [clause]
    words = clause.split(" ")
    out: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > max_chars:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return out


def _split_clauses(text: str, max_chars: int) -> list[str]:
    raw = [part.strip() for part in _CLAUSE_SPLIT_RE.split(text) if part.strip()] or [text]
    expanded: list[str] = []
    for part in raw:
        expanded.extend(_wrap_long(part, max_chars))
    merged: list[str] = []
    for part in expanded:
        if merged and len(merged[-1]) < _MIN_CUE_CHARS:
            merged[-1] = f"{merged[-1]} {part}".strip()
        else:
            merged.append(part)
    return merged


def _segment_timeline(segs: list[AsrSegment]) -> list[tuple[float, float, float, float]]:
    """Map source-char fractions [0,1] to real times, weighted per ASR segment."""
    total = sum(max(1, len(s.text_zh)) for s in segs)
    spans: list[tuple[float, float, float, float]] = []
    acc = 0
    for seg in segs:
        weight = max(1, len(seg.text_zh))
        spans.append((acc / total, (acc + weight) / total, seg.start, seg.end))
        acc += weight
    return spans


def _time_at(frac: float, timeline: list[tuple[float, float, float, float]], side: str) -> float:
    """A cue *start* belongs to the next segment (waits for that speech to begin,
    not the preceding gap); a cue *end* belongs to the current segment."""
    for index, (frac_start, frac_end, t_start, t_end) in enumerate(timeline):
        last = index == len(timeline) - 1
        contains = (frac < frac_end or last) if side == "start" else (frac <= frac_end)
        if contains:
            local = (frac - frac_start) / max(1e-9, frac_end - frac_start)
            return t_start + min(max(local, 0.0), 1.0) * (t_end - t_start)
    return timeline[-1][3]


def build_display_cues(
    speech_rows: list[SpeechRow],
    min_duration: float = 0.4,
    anchors: list[Anchor] | None = None,
    segments: list[AsrSegment] | None = None,
    max_chars: int = 42,
) -> list[dict]:
    anchor_by_id = {a.id: a for a in (anchors or [])}
    seg_by_id = {s.id: s for s in (segments or [])}

    by_anchor: dict[int, list[SpeechRow]] = {}
    order: list[int] = []
    for row in speech_rows:
        if row.anchor_id not in by_anchor:
            order.append(row.anchor_id)
        by_anchor.setdefault(row.anchor_id, []).append(row)

    cues: list[dict] = []
    prev_end = 0.0
    for anchor_id in order:
        rows = by_anchor[anchor_id]
        anchor = anchor_by_id.get(anchor_id)
        vi_text = strip_explicit_turn_markers(" ".join(r.text for r in rows))
        if not vi_text:
            continue
        anchor_start, anchor_end = rows[0].anchor_start, rows[0].anchor_end

        segs = []
        if anchor:
            lo, hi = anchor.source_id_range()
            segs = [seg_by_id[i] for i in range(lo, hi + 1) if i in seg_by_id]

        clauses = _split_clauses(vi_text, max_chars)
        timeline = _segment_timeline(segs) if segs else []
        total_chars = sum(max(1, len(c)) for c in clauses)

        cursor = 0
        for index, clause in enumerate(clauses):
            frac0 = cursor / total_chars
            cursor += max(1, len(clause))
            frac1 = cursor / total_chars
            if timeline:
                cue_start = anchor_start if index == 0 else _time_at(frac0, timeline, "start")
                cue_end = anchor_end if index == len(clauses) - 1 else _time_at(frac1, timeline, "end")
            else:
                window = max(min_duration, anchor_end - anchor_start)
                cue_start = anchor_start + window * frac0
                cue_end = anchor_end if index == len(clauses) - 1 else anchor_start + window * frac1

            cue_start = max(cue_start, prev_end)
            cue_end = max(cue_end, cue_start + min_duration)
            cues.append(
                {
                    "start": round(cue_start, 3),
                    "end": round(cue_end, 3),
                    "text": clause,
                    "translated": clause,
                    "source": anchor.source_zh if anchor else "",
                    "anchor_id": anchor_id,
                    "source_ids": rows[0].source_ids,
                }
            )
            prev_end = cue_end

    _close_micro_gaps(cues)
    return cues


def _close_micro_gaps(cues: list[dict], micro_gap: float = 0.3) -> None:
    """Hold a cue until the next one appears when the gap is tiny, to avoid the
    subtitle flickering off and back on for a fraction of a second."""
    for prev, cur in zip(cues, cues[1:]):
        gap = cur["start"] - prev["end"]
        if 0 < gap <= micro_gap:
            prev["end"] = cur["start"]


def write_subtitles(cues: list[dict], json_path: Path, srt_path: Path, vtt_path: Path | None = None) -> None:
    write_json(json_path, cues)
    write_srt(cues, srt_path)
    if vtt_path is not None:
        write_vtt(cues, vtt_path)
