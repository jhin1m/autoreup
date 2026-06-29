"""Build the ASR rail: micro-segments and the anchor timing windows over them.

ASR segments are the hard rail and are preserved exactly. Anchors group a
contiguous run of segments into a timing window, bounded by duration, segment
count, and source-char budget. An anchor's ``source_ids`` always covers the full
segment range so downstream stages can recover micro-beat boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import AsrSegment, Anchor
from .text import clean
from .tsv_io import parse_srt


@dataclass(slots=True)
class AnchorConfig:
    max_anchor_duration: float = 12.0
    max_anchor_segments: int = 6
    max_anchor_source_chars: int = 60


def segments_from_srt(path: Path) -> list[AsrSegment]:
    return [
        AsrSegment(id=row["id"], start=row["start"], end=row["end"], text_zh=clean(row["text"]))
        for row in parse_srt(path)
    ]


def build_anchors(segments: list[AsrSegment], config: AnchorConfig | None = None) -> list[Anchor]:
    config = config or AnchorConfig()
    anchors: list[Anchor] = []
    current: list[AsrSegment] = []

    def flush() -> None:
        if not current:
            return
        anchors.append(
            Anchor(
                id=len(anchors),
                start=current[0].start,
                end=current[-1].end,
                source_ids=f"{current[0].id}-{current[-1].id}",
                source_zh="".join(seg.text_zh for seg in current),
            )
        )
        current.clear()

    for seg in segments:
        if current:
            duration = seg.end - current[0].start
            text_len = sum(len(item.text_zh) for item in (*current, seg))
            if (
                duration > config.max_anchor_duration
                or len(current) >= config.max_anchor_segments
                or text_len > config.max_anchor_source_chars
            ):
                flush()
        current.append(seg)
    flush()
    return anchors
