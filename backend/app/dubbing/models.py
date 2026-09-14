"""Typed data contract for the dubbing pipeline.

Stages exchange these dataclasses in-process. The ``to_row``/``from_row``
helpers exist only to persist artifacts as TSV for inspection and resume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _fmt(seconds: float) -> str:
    return f"{float(seconds):.3f}"


@dataclass(slots=True)
class AsrSegment:
    """A single ASR micro-beat. The hard rail; never merged away."""

    id: int
    start: float
    end: float
    text_zh: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_row(self) -> dict[str, str]:
        return {"id": str(self.id), "start": _fmt(self.start), "end": _fmt(self.end), "text": self.text_zh}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AsrSegment":
        return cls(int(row["id"]), float(row["start"]), float(row["end"]), str(row.get("text", "")))


@dataclass(slots=True)
class Anchor:
    """A timing window over a contiguous range of ASR segments."""
    id: int
    start: float
    end: float
    source_ids: str  # inclusive "lo-hi" range over AsrSegment ids
    source_zh: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def source_id_range(self) -> tuple[int, int]:
        lo, _, hi = self.source_ids.partition("-")
        lo_i = int(lo) if lo.strip() else 0
        hi_i = int(hi) if hi.strip() else lo_i
        return lo_i, hi_i

    def to_row(self) -> dict[str, str]:
        return {
            "ID": str(self.id),
            "START": _fmt(self.start),
            "END": _fmt(self.end),
            "SOURCE_IDS": self.source_ids,
            "SOURCE_ZH": self.source_zh,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Anchor":
        return cls(
            int(row["ID"]),
            float(row["START"]),
            float(row["END"]),
            str(row.get("SOURCE_IDS", "")),
            str(row.get("SOURCE_ZH", "")),
        )


@dataclass(slots=True)
class Scene:
    """A narrative span emitted by the (optional) scene pass or flat fallback."""

    scene_id: str
    start_anchor: int
    end_anchor: int
    label: str = "Scene"
    tone: str = ""
    summary: str = ""
    must_keep: str = ""
    pacing: str = ""
    handoff: str = ""

    def to_row(self) -> dict[str, str]:
        return {
            "SCENE_ID": self.scene_id,
            "START_ANCHOR": str(self.start_anchor),
            "END_ANCHOR": str(self.end_anchor),
            "LABEL": self.label,
            "TONE": self.tone,
            "SUMMARY": self.summary,
            "MUST_KEEP": self.must_keep,
            "PACING": self.pacing,
            "HANDOFF": self.handoff,
        }


@dataclass(slots=True)
class Unit:
    """A semantic translation unit (sentence/paragraph) built from the ASR rail.

    ``segment_ids`` keeps the inclusive ``lo-hi`` range over AsrSegment ids so the
    unit's timing is inherited from the rail — units are a translation concept,
    never a timing source of their own."""

    id: int
    segment_ids: str
    start: float
    end: float
    source_zh: str
    flags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Run:
    """A stretch of continuous speech: consecutive units whose internal gap is
    below the run gap (default 0.8s). Runs are the only places the voice timeline
    anchors to the picture."""

    id: int
    unit_ids: list[int]
    start: float
    end: float


@dataclass(slots=True)
class Chunk:
    """A duration-bounded group of scenes fed to the translator as one unit."""

    id: int
    start_anchor: int
    end_anchor: int
    start: float
    end: float
    scene_ids: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_row(self) -> dict[str, str]:
        return {
            "CHUNK_ID": str(self.id),
            "START_ANCHOR": str(self.start_anchor),
            "END_ANCHOR": str(self.end_anchor),
            "START": _fmt(self.start),
            "END": _fmt(self.end),
            "DURATION_S": f"{self.duration:.3f}",
            "SCENE_IDS": ",".join(self.scene_ids),
            "LABELS": " | ".join(self.labels),
        }


@dataclass(slots=True)
class SpeechRow:
    """The central artifact: one Vietnamese cue spoken + displayed."""

    sub_id: int
    anchor_id: int
    anchor_start: float
    anchor_end: float
    source_ids: str
    text: str

    @property
    def anchor_duration(self) -> float:
        return max(0.0, self.anchor_end - self.anchor_start)

    def to_row(self) -> dict[str, str]:
        return {
            "SUB_ID": str(self.sub_id),
            "ANCHOR_ID": str(self.anchor_id),
            "ANCHOR_START": _fmt(self.anchor_start),
            "ANCHOR_END": _fmt(self.anchor_end),
            "SOURCE_IDS": self.source_ids,
            "TEXT": self.text,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "SpeechRow":
        return cls(
            int(row["SUB_ID"]),
            int(row["ANCHOR_ID"]),
            float(row["ANCHOR_START"]),
            float(row["ANCHOR_END"]),
            str(row.get("SOURCE_IDS", "")),
            str(row.get("TEXT", "")),
        )


@dataclass(slots=True)
class GlossaryEntry:
    term_zh: str
    term_vi: str
    kind: str = ""  # name | term | place | rank | ...
    note: str = ""


ANCHOR_FIELDS = ["ID", "START", "END", "SOURCE_IDS", "SOURCE_ZH"]
SEGMENT_FIELDS = ["id", "start", "end", "text"]
CHUNK_FIELDS = ["CHUNK_ID", "START_ANCHOR", "END_ANCHOR", "START", "END", "DURATION_S", "SCENE_IDS", "LABELS"]
SCENE_FIELDS = ["SCENE_ID", "START_ANCHOR", "END_ANCHOR", "LABEL", "TONE", "SUMMARY", "MUST_KEEP", "PACING", "HANDOFF"]
SPEECH_FIELDS = ["SUB_ID", "ANCHOR_ID", "ANCHOR_START", "ANCHOR_END", "SOURCE_IDS", "TEXT"]
