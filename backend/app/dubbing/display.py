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

import math
import re
from pathlib import Path

from backend.app.services.subtitle_formats import write_srt, write_vtt
from .models import Anchor, AsrSegment, SpeechRow
from .text import strip_explicit_turn_markers
from .tsv_io import write_json

_MIN_CUE_CHARS = 6
_DISPLAY_MIN_DUR = 0.9  # pro: a line should stay on screen long enough to read (~5/6s..1s)
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[,.!?;…，。！？；])\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…。！？])\s+")


def _split_sentences(text: str) -> list[str]:
    """Split at sentence ends only (. ! ? …) — NEVER mid-sentence. A sentence is the
    atomic unit for the voice so a continuous sentence is never cut into two clips."""
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return parts or [text]


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


_CLAUSE_END_RE = re.compile(r"[,.!?;:…，。！？；：]$")


# Vietnamese connector/function words: a line may break BEFORE one of these (it
# starts a new phrase) — that keeps two-syllable words ("chuẩn bị", "linh tuyền",
# "quan sát") intact instead of splitting them across lines (a subtitle antipattern).
_BREAK_BEFORE = {
    "và", "với", "rồi", "để", "mà", "nhưng", "hoặc", "vào", "ra", "lên", "xuống",
    "qua", "trong", "ngoài", "trên", "dưới", "của", "cho", "từ", "đến", "tới",
    "khi", "nếu", "vì", "do", "bởi", "nên", "thì", "sau", "trước", "rằng", "là",
    "cùng", "theo", "bằng", "về", "đã", "sẽ", "đang", "cũng", "vẫn", "còn", "hay",
}


def _chunk_line(text: str, min_chars: int, max_chars: int) -> list[str]:
    """Pro single-line chunking. A sentence that fits one line is kept WHOLE. A longer
    one is split ONLY at phrase boundaries — before a connector word or after clause
    punctuation — so a two-syllable Vietnamese word is never cut across lines. Within
    a line we fill up to ``max_chars`` and break at the last phrase boundary that fits;
    only if a phrase has no inner boundary do we fall back to a plain word break."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return [text]
    words = text.split(" ")

    def breakable_before(i: int) -> bool:
        if i <= 0:
            return False
        if words[i].lower().strip(",.;:!?…") in _BREAK_BEFORE:
            return True
        return words[i - 1][-1:] in ",.;:…"  # right after a clause mark

    hard_max = max_chars + 14  # a line may run over this far to reach a safe break
    breaks = [i for i in range(1, len(words)) if breakable_before(i)]

    def width(a: int, b: int) -> int:
        return len(" ".join(words[a:b]))

    chunks: list[str] = []
    start = 0
    while start < len(words):
        cands = [i for i in breaks if i > start]
        if width(start, len(words)) <= max_chars:
            end = len(words)                                   # rest fits one line
        else:
            fits = [i for i in cands if width(start, i) <= max_chars]
            if fits:
                end = max(fits)                                # latest boundary that fits
            else:
                over = [i for i in cands if width(start, i) <= hard_max]
                if over:
                    end = min(over)                            # overflow to nearest boundary
                else:
                    end = start + 1                            # last resort: plain word break
                    while end < len(words) and width(start, end + 1) <= max_chars:
                        end += 1
        chunks.append(" ".join(words[start:end]))
        start = end

    # orphan guard: a tiny tail (a lone particle) merges back onto the previous line,
    # even if it then runs a little long — a 48-char line beats an orphan word.
    if len(chunks) >= 2 and len(chunks[-1]) <= 6 and len(chunks[-2]) + 1 + len(chunks[-1]) <= max_chars + 8:
        chunks[-2] = f"{chunks[-2]} {chunks[-1]}"
        chunks.pop()
    return chunks or [text]


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


def build_timed_units(
    speech_rows: list[SpeechRow],
    min_duration: float = 0.4,
    anchors: list[Anchor] | None = None,
    segments: list[AsrSegment] | None = None,
) -> list[dict]:
    """Split each anchor's Vietnamese into SENTENCES, each timed to the source ASR
    segments (char-proportional).

    A sentence is the atomic unit: it is the thing the voice synthesizes as ONE
    continuous clip and places as ONE contiguous block, so a continuous sentence is
    never cut into two TTS clips nor scattered across distant source-segment times
    (which happened at clause granularity when VI/ZH word order differed). Each unit
    is ``{anchor_id, start, end, text, source, source_ids}``. Falls back to a
    proportional spread over the anchor window when segment timing isn't available."""
    anchor_by_id = {a.id: a for a in (anchors or [])}
    seg_by_id = {s.id: s for s in (segments or [])}

    by_anchor: dict[int, list[SpeechRow]] = {}
    order: list[int] = []
    for row in speech_rows:
        if row.anchor_id not in by_anchor:
            order.append(row.anchor_id)
        by_anchor.setdefault(row.anchor_id, []).append(row)

    units: list[dict] = []
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

        sentences = _split_sentences(vi_text)
        timeline = _segment_timeline(segs) if segs else []
        total_chars = sum(max(1, len(s)) for s in sentences)

        cursor = 0
        for index, sentence in enumerate(sentences):
            frac0 = cursor / total_chars
            cursor += max(1, len(sentence))
            frac1 = cursor / total_chars
            if timeline:
                u_start = anchor_start if index == 0 else _time_at(frac0, timeline, "start")
                u_end = anchor_end if index == len(sentences) - 1 else _time_at(frac1, timeline, "end")
            else:
                window = max(min_duration, anchor_end - anchor_start)
                u_start = anchor_start + window * frac0
                u_end = anchor_end if index == len(sentences) - 1 else anchor_start + window * frac1

            u_start = max(u_start, prev_end)
            u_end = max(u_end, u_start + min_duration)
            units.append(
                {
                    "start": round(u_start, 3),
                    "end": round(u_end, 3),
                    "text": sentence,
                    "source": anchor.source_zh if anchor else "",
                    "anchor_id": anchor_id,
                    "source_ids": rows[0].source_ids,
                }
            )
            prev_end = u_end
    return units


def build_display_cues(
    speech_rows: list[SpeechRow],
    min_duration: float = 0.4,
    anchors: list[Anchor] | None = None,
    segments: list[AsrSegment] | None = None,
    max_chars: int = 42,
    voice_windows: dict[float, tuple[float, float]] | None = None,
    lead: float = 0.0,
) -> list[dict]:
    """Subtitle = each sentence-unit subdivided into readable lines, timed WITHIN the
    sentence's real spoken span. ``voice_windows`` maps a unit's source-start (rounded
    to 3dp) to the actual [placement, placement+voice_duration] the synthesized clip
    occupies on the timeline; the lines are spread char-proportionally across THAT, so
    the caption matches the voice's real length/position (from the TTS), not the
    source window. Falls back to the source window when a clip span isn't available."""
    voice_windows = voice_windows or {}
    cues: list[dict] = []
    prev_end = 0.0
    for unit in build_timed_units(speech_rows, min_duration, anchors, segments):
        base, top = voice_windows.get(round(unit["start"], 3), (unit["start"], unit["end"]))
        span = max(min_duration, top - base)
        # Duration-aware single-line chunking: size each line so it holds for at least
        # _DISPLAY_MIN_DUR at this sentence's speaking rate (chars/sec), capped at the
        # readable line width — so chunks never flash by too fast to read.
        cps = len(unit["text"]) / span
        min_chars = max(_MIN_CUE_CHARS, int(_DISPLAY_MIN_DUR * cps))
        lines = _chunk_line(unit["text"], min_chars, max_chars)
        total_chars = sum(max(1, len(line)) for line in lines)
        cursor = 0
        for index, line in enumerate(lines):
            frac0 = cursor / total_chars
            cursor += max(1, len(line))
            frac1 = cursor / total_chars
            cue_start = base if index == 0 else base + span * frac0
            cue_end = top if index == len(lines) - 1 else base + span * frac1
            cue_start = max(cue_start, prev_end)
            cue_end = max(cue_end, cue_start + min_duration)
            cues.append(
                {
                    "start": round(cue_start, 3),
                    "end": round(cue_end, 3),
                    "text": line,
                    "translated": line,
                    "source": unit["source"],
                    "anchor_id": unit["anchor_id"],
                    "source_ids": unit["source_ids"],
                }
            )
            prev_end = cue_end
    _enforce_min_duration(cues)
    _apply_lead(cues, lead)
    _close_micro_gaps(cues)
    return cues


def _apply_lead(cues: list[dict], lead: float) -> None:
    """Pro lead-in: bring each caption on screen ``lead`` seconds BEFORE its voice so
    the eye can start reading — but never before the previous cue ends (no overlap),
    so back-to-back lines get no lead and only onsets after a pause move earlier."""
    if lead <= 0:
        return
    for index, cue in enumerate(cues):
        floor = cues[index - 1]["end"] if index > 0 else 0.0
        cue["start"] = round(max(floor, cue["start"] - lead), 3)


def _enforce_min_duration(cues: list[dict], target: float = 0.9) -> None:
    """Hold any too-short cue on screen up to ``target`` seconds — but never past the
    next cue's start (so it can't overlap). The voice may have finished; the caption
    lingering a beat into a pause is correct pro behaviour and stops the flashing."""
    for index, cue in enumerate(cues):
        if cue["end"] - cue["start"] >= target:
            continue
        ceiling = cues[index + 1]["start"] if index + 1 < len(cues) else cue["start"] + target
        cue["end"] = round(min(max(cue["end"], cue["start"] + target), ceiling), 3)


def _close_micro_gaps(cues: list[dict], micro_gap: float = 0.8) -> None:
    """Hold a cue until the next one appears when the gap is short, to avoid the
    subtitle flickering off and back on between sentences (a real scene pause beyond
    this threshold still clears the caption)."""
    for prev, cur in zip(cues, cues[1:]):
        gap = cur["start"] - prev["end"]
        if 0 < gap <= micro_gap:
            prev["end"] = cur["start"]


def write_subtitles(cues: list[dict], json_path: Path, srt_path: Path, vtt_path: Path | None = None) -> None:
    write_json(json_path, cues)
    write_srt(cues, srt_path)
    if vtt_path is not None:
        write_vtt(cues, vtt_path)
