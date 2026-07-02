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
    max_anchor_duration: float = 8.0
    max_anchor_segments: int = 6
    max_anchor_source_chars: int = 60
    # A silence gap this long between segments is treated as a phrase/sentence
    # boundary and starts a new anchor — so each anchor is a natural speech unit
    # the voice-over can align to (dubbing), not an arbitrary multi-sentence span.
    max_gap: float = 0.45
    # When a size limit (chars/duration/segments) forces a cut, back up to the
    # widest pause inside the run instead of cutting at the limit position. The ASR
    # has no punctuation, so inter-segment silence is the ONLY sentence signal —
    # a hard cut at "60 chars" regularly landed mid-sentence and the translation
    # of both halves came out choppy. Pauses below this are not usable boundaries.
    min_split_gap: float = 0.08


def segments_from_srt(path: Path) -> list[AsrSegment]:
    return [
        AsrSegment(id=row["id"], start=row["start"], end=row["end"], text_zh=clean(row["text"]))
        for row in parse_srt(path)
    ]


def build_anchors(segments: list[AsrSegment], config: AnchorConfig | None = None) -> list[Anchor]:
    config = config or AnchorConfig()
    anchors: list[Anchor] = []
    current: list[AsrSegment] = []

    def emit(run: list[AsrSegment]) -> None:
        if not run:
            return
        anchors.append(
            Anchor(
                id=len(anchors),
                start=run[0].start,
                end=run[-1].end,
                source_ids=f"{run[0].id}-{run[-1].id}",
                source_zh="".join(seg.text_zh for seg in run),
            )
        )

    def flush_at_best_gap() -> None:
        """Cut the run at its widest internal pause; the tail seeds the next anchor.
        Falls back to a full flush when the run has no usable pause."""
        best, best_gap = 0, config.min_split_gap
        for j in range(1, len(current)):
            gap = current[j].start - current[j - 1].end
            if gap >= best_gap:
                best, best_gap = j, gap
        if best > 0:
            emit(current[:best])
            del current[:best]
        else:
            emit(current)
            current.clear()

    for seg in segments:
        if current:
            gap = seg.start - current[-1].end
            duration = seg.end - current[0].start
            text_len = sum(len(item.text_zh) for item in (*current, seg))
            if gap > config.max_gap:
                emit(current)  # a real pause IS the boundary — no backtracking needed
                current.clear()
            elif (
                duration > config.max_anchor_duration
                or len(current) >= config.max_anchor_segments
                or text_len > config.max_anchor_source_chars
            ):
                flush_at_best_gap()
        current.append(seg)
    emit(current)
    return anchors
