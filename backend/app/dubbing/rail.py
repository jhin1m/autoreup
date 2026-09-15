"""ASR rail helpers: parse the worker's SRT into micro-segments.

ASR/OCR captions are the hard timing rail — preserved exactly, never merged away.
Each caption becomes one translated row (``translate``) and one placed voice clip
(``timeline.plan_rows``); semantic units (``units.build_units``) group captions for
translation context only.
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
