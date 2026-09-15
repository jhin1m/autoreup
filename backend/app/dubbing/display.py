"""Turn speech rows into one-line display subtitle cues (F4: sub == voice, in time).

One row = one semantic unit's Vietnamese text (``translate``). A cue block covers
what the viewer must be able to read:

* the VOICE — the block starts at ``voice_start - lead`` (the eye gets a lead-in),
  so the words appear exactly while they are read;
* the SOURCE window — the block always ends no earlier than the unit's own window
  end, because the original caption is on screen for that long (the original band
  is covered, so an early-blinking subtitle would leave it blank mid-line).

Inside the block the lines are reflowed at safe break points and the non-final
lines are spread across the spoken span (char share of the measured voice), with
the last line holding to the block end. Blocks are clamped so no cue overlaps the
next. No cross-segment interpolation, no separate hold pass: the union above IS
the rule.
"""

from __future__ import annotations

from pathlib import Path

from backend.app.services.subtitle_formats import write_srt, write_vtt
from .models import AsrSegment, SpeechRow
from .text import sanitize_caption, strip_explicit_turn_markers
from .tsv_io import write_json

# A display cue may exceed its target width this far to reach a safe break (never
# splitting a two-syllable word). QA gates at the same allowance.
LINE_SLACK = 8

# Vietnamese connector/function words: a line may break BEFORE one of these (it
# starts a new phrase) — that keeps two-syllable words ("chuẩn bị", "linh tuyền",
# "quan sát") intact instead of splitting them across lines (a subtitle antipattern).
_BREAK_BEFORE = {
    "và", "với", "rồi", "để", "mà", "nhưng", "hoặc", "vào", "ra", "lên", "xuống",
    "qua", "trong", "ngoài", "trên", "dưới", "của", "cho", "từ", "đến", "tới",
    "khi", "nếu", "vì", "do", "bởi", "nên", "thì", "sau", "trước", "rằng", "là",
    "cùng", "theo", "bằng", "về", "đã", "sẽ", "đang", "cũng", "vẫn", "còn", "hay",
}


def _chunk_line(text: str, max_chars: int) -> list[str]:
    """Pro single-line chunking. MIRRORS the rule the translate skill tells (rule 5
    in the translate skill, same ``max_caption_chars``): a sentence/clause that fits one
    line is kept WHOLE; a longer one breaks at PUNCTUATION first (clause marks), then at
    a connector word, so a two-syllable Vietnamese word is never cut across lines. Only
    a long run with NO punctuation/connector inside falls back to a plain word break —
    which is exactly the case the prompt tells the agent to avoid by adding a comma."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return [text]
    words = text.split(" ")

    # Two break classes, in priority order. A clause break (right after a comma/clause
    # mark) is the natural, punctuation-aligned place to wrap and is preferred. Only if
    # no clause break fits the line do we fall back to a connector break (before a
    # function word) — which still keeps a 2-syllable word intact but is less ideal
    # (it can sit mid-clause, e.g. "...cơm mang | về xét nghiệm").
    def is_clause_break(i: int) -> bool:
        return i > 0 and words[i - 1][-1:] in ",.;:…"

    def is_connector_break(i: int) -> bool:
        return i > 0 and words[i].lower().strip(",.;:!?…\"”") in _BREAK_BEFORE

    # A capitalized word AFTER a lowercase one starts a proper-name run ("…phủ nhận
    # giới thiệu | Lâm Quốc Đống…") — breaking there is safe, whereas the plain
    # word-break fallback can land INSIDE a two-syllable word ("giới | thiệu"). The
    # previous-word check keeps the break at the START of the run, never inside a
    # multi-word name ("Triệu | Dũng").
    def is_capital_break(i: int) -> bool:
        head = words[i].lstrip("\"“”'([")
        prev = words[i - 1].lstrip("\"“”'([") if i > 0 else ""
        return i > 0 and head[:1].isupper() and not prev[:1].isupper()

    hard_max = max_chars + LINE_SLACK  # over this far only to reach a safe break
    clause_breaks = [i for i in range(1, len(words)) if is_clause_break(i)]
    conn_breaks = [i for i in range(1, len(words)) if is_connector_break(i)]
    cap_breaks = [i for i in range(1, len(words)) if is_capital_break(i)]

    def width(a: int, b: int) -> int:
        return len(" ".join(words[a:b]))

    chunks: list[str] = []
    start = 0
    while start < len(words):
        if width(start, len(words)) <= max_chars:
            end = len(words)                                   # rest fits one line
        else:
            clause_fits = [i for i in clause_breaks if i > start and width(start, i) <= max_chars]
            conn_fits = [i for i in conn_breaks if i > start and width(start, i) <= max_chars]
            cap_fits = [i for i in cap_breaks if i > start and width(start, i) <= max_chars]
            if clause_fits:
                end = max(clause_fits)                         # prefer a punctuation break
            elif conn_fits:
                end = max(conn_fits)                           # else a connector boundary
            elif cap_fits:
                end = max(cap_fits)                            # else before a proper name
            else:
                over = [i for i in (clause_breaks + conn_breaks + cap_breaks) if i > start and width(start, i) <= hard_max]
                if over:
                    end = min(over)                            # overflow to nearest safe break
                else:
                    end = start + 1                            # last resort: plain word break
                    while end < len(words) and width(start, end + 1) <= max_chars:
                        end += 1
        chunks.append(" ".join(words[start:end]))
        start = end

    # orphan guard: a stubby head/tail ("Doanh số", a lone "không?") merges into its
    # neighbour even if the line then runs a little long — one slightly-wide line
    # beats a fragment flashing on screen for a fraction of a second.
    if len(chunks) >= 2 and len(chunks[0]) <= 14 and len(chunks[0]) + 1 + len(chunks[1]) <= max_chars + 8:
        chunks[1] = f"{chunks[0]} {chunks[1]}"
        chunks.pop(0)
    if len(chunks) >= 2 and len(chunks[-1]) <= 14 and len(chunks[-2]) + 1 + len(chunks[-1]) <= max_chars + 8:
        chunks[-2] = f"{chunks[-2]} {chunks[-1]}"
        chunks.pop()
    return chunks or [text]


def build_display_cues(
    speech_rows: list[SpeechRow],
    segments: list[AsrSegment] | None = None,
    min_duration: float = 0.4,
    max_chars: int = 42,
    lead: float = 0.0,
    voice_windows: dict[int, tuple[float, float]] | None = None,
) -> list[dict]:
    """One cue block per unit covering the voice and the source window (see module doc)."""
    voice_windows = voice_windows or {}
    seg_by_id = {s.id: s for s in (segments or [])}
    blocks: list[dict] = []
    for row in speech_rows:
        text = sanitize_caption(strip_explicit_turn_markers(row.text))
        if not text:
            continue
        base, top = voice_windows.get(row.sub_id, (row.anchor_start, row.anchor_end))
        start = min(base, row.anchor_start) - lead
        end = max(top, row.anchor_end)
        blocks.append({"row": row, "base": base, "top": top, "start": start, "end": end, "text": text})

    # clamp blocks: a cue never runs into the next one
    for index in range(len(blocks) - 1):
        blocks[index]["end"] = min(blocks[index]["end"], blocks[index + 1]["start"])
    for block in blocks:
        block["end"] = max(block["end"], block["start"] + min_duration)

    cues: list[dict] = []
    prev_end = 0.0
    for block in blocks:
        row = block["row"]
        source = ""
        lo, _, _hi = row.source_ids.partition("-")
        first = seg_by_id.get(int(lo)) if lo.strip().isdigit() else None
        if first is not None:
            source = first.text_zh
        lines = _chunk_line(block["text"], max_chars)
        total_chars = sum(max(1, len(line)) for line in lines)
        span = max(min_duration, block["top"] - block["base"])
        cursor = 0
        for index, line in enumerate(lines):
            frac0 = cursor / total_chars
            cursor += max(1, len(line))
            frac1 = cursor / total_chars
            cue_start = block["start"] if index == 0 else block["base"] + span * frac0
            cue_end = block["end"] if index == len(lines) - 1 else block["base"] + span * frac1
            cue_start = max(cue_start, prev_end)
            cue_end = max(cue_end, cue_start + min_duration)
            cues.append(
                {
                    "start": round(cue_start, 3),
                    "end": round(cue_end, 3),
                    "text": line,
                    "translated": line,
                    "source": source,
                    "anchor_id": row.anchor_id,
                    "source_ids": row.source_ids,
                }
            )
            prev_end = cue_end
    _enforce_min_duration(cues)
    _close_micro_gaps(cues)
    return cues


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
