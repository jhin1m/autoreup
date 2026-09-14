"""ASR rail helpers: parse the worker's SRT into micro-segments.

ASR micro-segments are the hard timing rail — they are preserved exactly and are
never merged away. Semantic units (``units.build_units``) are built on top of them
for translation; the timeline (``timeline.plan_timeline``) groups them into runs.
"""

from __future__ import annotations

from pathlib import Path

from .models import AsrSegment
from .text import clean
from .tsv_io import parse_srt


def segments_from_srt(path: Path) -> list[AsrSegment]:
    return [
        AsrSegment(id=row["id"], start=row["start"], end=row["end"], text_zh=clean(row["text"]))
        for row in parse_srt(path)
    ]
