"""Turn speech rows into one-line display subtitle cues.

Cues within an anchor share the anchor window; we distribute that window across
them proportionally to text length so subtitles track the voice. Output is the
cue-dict format the SRT/VTT writers and QA expect.
"""

from __future__ import annotations

from pathlib import Path

from backend.app.services.subtitle_formats import write_srt, write_vtt
from .models import Anchor, SpeechRow
from .text import strip_explicit_turn_markers
from .tsv_io import write_json


def build_display_cues(
    speech_rows: list[SpeechRow], min_duration: float = 0.4, anchors: list[Anchor] | None = None
) -> list[dict]:
    source_by_anchor = {a.id: a.source_zh for a in (anchors or [])}
    by_anchor: dict[int, list[SpeechRow]] = {}
    order: list[int] = []
    for row in speech_rows:
        if row.anchor_id not in by_anchor:
            order.append(row.anchor_id)
        by_anchor.setdefault(row.anchor_id, []).append(row)

    cues: list[dict] = []
    for anchor_id in order:
        rows = by_anchor[anchor_id]
        start = rows[0].anchor_start
        end = rows[0].anchor_end
        window = max(min_duration, end - start)
        total_chars = sum(max(1, len(strip_explicit_turn_markers(r.text))) for r in rows)
        cursor = start
        for index, row in enumerate(rows):
            text = strip_explicit_turn_markers(row.text)
            frac = max(1, len(text)) / total_chars
            cue_end = end if index == len(rows) - 1 else min(end, cursor + window * frac)
            cues.append(
                {
                    "start": round(cursor, 3),
                    "end": round(max(cue_end, cursor + 0.001), 3),
                    "text": text,
                    "translated": text,
                    "source": source_by_anchor.get(anchor_id, ""),
                    "anchor_id": anchor_id,
                    "source_ids": row.source_ids,
                }
            )
            cursor = cue_end
    return cues


def write_subtitles(cues: list[dict], json_path: Path, srt_path: Path, vtt_path: Path | None = None) -> None:
    write_json(json_path, cues)
    write_srt(cues, srt_path)
    if vtt_path is not None:
        write_vtt(cues, vtt_path)
