"""Turn speech rows into one-line display subtitle cues.

Cue timing is mapped onto the ASR micro-segment rail, not spread evenly over the
anchor window: a cue boundary at character-fraction f of an anchor's Vietnamese
text is placed at the time where fraction f of the source speech is actually
spoken (interpolated across the constituent ASR segments, honouring the gaps
between them). This keeps subtitles from running ahead of the original voice.
Falls back to proportional spread when segment timing isn't available.
"""

from __future__ import annotations

from pathlib import Path

from backend.app.services.subtitle_formats import write_srt, write_vtt
from .models import Anchor, AsrSegment, SpeechRow
from .text import strip_explicit_turn_markers
from .tsv_io import write_json


def _segment_timeline(anchor: Anchor, seg_by_id: dict[int, AsrSegment]) -> list[tuple[float, float, float, float]]:
    """Map source-char fractions [0,1] of an anchor to real times via its ASR segments.

    Returns ``(frac_start, frac_end, time_start, time_end)`` spans weighted by each
    segment's Chinese character count, so the gaps/pacing of actual speech are kept.
    """
    lo, hi = anchor.source_id_range()
    segs = [seg_by_id[i] for i in range(lo, hi + 1) if i in seg_by_id]
    if not segs:
        return []
    total = sum(max(1, len(s.text_zh)) for s in segs)
    spans: list[tuple[float, float, float, float]] = []
    acc = 0
    for seg in segs:
        weight = max(1, len(seg.text_zh))
        spans.append((acc / total, (acc + weight) / total, seg.start, seg.end))
        acc += weight
    return spans


def _time_at(frac: float, timeline: list[tuple[float, float, float, float]], side: str) -> float:
    """Map a char-fraction to a time. ``side`` decides boundary behaviour: a cue
    *start* belongs to the next segment (so it waits for that speech to begin,
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
    for anchor_id in order:
        rows = by_anchor[anchor_id]
        anchor = anchor_by_id.get(anchor_id)
        start = rows[0].anchor_start
        end = rows[0].anchor_end
        window = max(min_duration, end - start)
        timeline = _segment_timeline(anchor, seg_by_id) if anchor else []

        texts = [strip_explicit_turn_markers(r.text) for r in rows]
        lengths = [max(1, len(t)) for t in texts]
        total = sum(lengths)

        cursor_chars = 0
        prev_end = start
        for index, (row, text, length) in enumerate(zip(rows, texts, lengths)):
            frac0 = cursor_chars / total
            frac1 = (cursor_chars + length) / total
            cursor_chars += length
            if timeline:
                cue_start = start if index == 0 else _time_at(frac0, timeline, "start")
                cue_end = end if index == len(rows) - 1 else _time_at(frac1, timeline, "end")
            else:
                cue_start = start + window * frac0
                cue_end = end if index == len(rows) - 1 else start + window * frac1

            cue_start = max(cue_start, prev_end)
            cue_end = max(cue_end, cue_start + min_duration)
            if index == len(rows) - 1:
                cue_end = max(end, cue_start + min_duration)
            cues.append(
                {
                    "start": round(cue_start, 3),
                    "end": round(cue_end, 3),
                    "text": text,
                    "translated": text,
                    "source": anchor.source_zh if anchor else "",
                    "anchor_id": anchor_id,
                    "source_ids": row.source_ids,
                }
            )
            prev_end = cue_end
    return cues


def write_subtitles(cues: list[dict], json_path: Path, srt_path: Path, vtt_path: Path | None = None) -> None:
    write_json(json_path, cues)
    write_srt(cues, srt_path)
    if vtt_path is not None:
        write_vtt(cues, vtt_path)
