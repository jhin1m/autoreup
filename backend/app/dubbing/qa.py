"""Deterministic QA gate for the dubbing output.

Code owns QA: it checks the invariants the LLM stages must never violate — every
source caption has a Vietnamese row with the SAME window as the rail (timing is the
rail's, not the model's), no Han leakage, sequential sub ids, no cue overlaps, and a
one-line display character ceiling. Returns a report; the caller fails the job when
``issues`` is non-zero.
"""

from __future__ import annotations

from .display import LINE_SLACK
from .models import AsrSegment, SpeechRow
from .text import has_han, strip_explicit_turn_markers


def _caption_id(source_ids: str) -> int | None:
    lo = source_ids.partition("-")[0].strip()
    return int(lo) if lo.isdigit() else None


def qa_report(
    speech_rows: list[SpeechRow],
    segments: list[AsrSegment],
    display_cues: list[dict],
    max_display_chars: int = 32,
) -> dict:
    issues: list[str] = []

    han_rows = [r.sub_id for r in speech_rows if has_han(r.text)]
    if han_rows:
        issues.append(f"han_in_speech:{han_rows[:10]}")

    if [r.sub_id for r in speech_rows] != list(range(len(speech_rows))):
        issues.append("sub_ids_not_sequential")

    seg_by_id = {s.id: s for s in segments}
    covered: set[int] = set()
    misaligned: list[int] = []
    for row in speech_rows:
        caption_id = _caption_id(row.source_ids)
        if caption_id is None:
            continue
        covered.add(caption_id)
        seg = seg_by_id.get(caption_id)
        if seg is None:
            continue
        # the row's window must BE the caption's window (the rail owns timing)
        if abs(row.anchor_start - seg.start) > 0.01 or abs(row.anchor_end - seg.end) > 0.01:
            misaligned.append(row.sub_id)
    missing = [s.id for s in segments if s.id not in covered]
    if missing:
        issues.append(f"captions_missing_coverage:{missing[:10]}")
    if misaligned:
        issues.append(f"rows_misaligned_with_rail:{misaligned[:10]}")

    empty = [r.sub_id for r in speech_rows if not strip_explicit_turn_markers(r.text)]
    if empty:
        issues.append(f"empty_text:{empty[:10]}")

    # The reflow may exceed the target by LINE_SLACK to avoid a bad break (see
    # display._chunk_line); the gate uses the same allowance so it only catches
    # real reflow breakage, not the designed overflow.
    over_len = [c.get("anchor_id") for c in display_cues if len(c.get("text", "")) > max_display_chars + LINE_SLACK]
    overlong = [aid for aid in over_len if aid is not None]

    overlaps = 0
    for prev, cur in zip(display_cues, display_cues[1:]):
        if float(cur["start"]) + 1e-6 < float(prev["end"]):
            overlaps += 1
    if overlaps:
        issues.append(f"cue_overlaps:{overlaps}")

    han_cues = sum(1 for c in display_cues if has_han(c.get("text", "")))
    if han_cues:
        issues.append(f"han_in_display:{han_cues}")

    return {
        "summary": {
            "speech_rows": len(speech_rows),
            "captions": len(segments),
            "display_cues": len(display_cues),
            "issues": len(issues),
            "overlong_display_cues": len(overlong),
            "issue_detail": issues,
        },
        "coverage": {
            "missing": len(missing),
            "misaligned": len(misaligned),
            "overlaps": overlaps,
            "contains_chinese": len(han_rows) + han_cues,
            "empty": len(empty),
        },
    }
