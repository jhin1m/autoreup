"""Voice timeline: one clip per source caption, re-anchored to the picture (L6).

The rail's caption windows ARE the picture timing — a caption is on screen exactly
when its content is on screen. Any timeline that reshuffles clips inside a speech
run drifts away from that: measured on a real episode the voice ran 23s ahead by
the end of a run and the last 24s of source dialogue had no dub and no subtitles
(the "có thoại mà không có sub" bug). Clamps and holds that hide the drift are
symptoms of the same wrong model, so this planner does the simple correct thing
per ROW:

1. re-anchor — ``start = max(previous voice end, caption.start - lead)``.
   A row whose predecessor finished early starts at its own picture again, so
   lateness cannot accumulate across rows; it can only come from a predecessor
   that genuinely overflowed (which the fit loop then shortens).
2. tempo — speed up only, never slow down, and only as much as needed to fit the
   free time before the next caption (``<= tempo_ceiling``).
3. report — overflow/underfill are measured per row and returned, never hidden.

4. stretch (optional) — a line still too long at the ceiling slows the PICTURE over
   its caption window (``[caption.start, next caption.start]``, <= ``stretch_max``)
   before the fit loop is asked to shorten the text. The cursor then runs in OUTPUT
   time while every returned field stays in SOURCE time (see ``time_warp``).

``underfill`` is not decided here: the caller grades the voice against the VAD
speech it must cover (``coverage.speech_seconds``), so a compact line that lands
inside a real source pause is not mistaken for dropped content.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import SpeechRow
from .time_warp import TimeWarp, WarpSegment


@dataclass(slots=True)
class TimelineConfig:
    lead: float = 0.15               # the voice may start this much before its caption
    tempo_ceiling: float = 1.18      # fastest speed-up used to fit a long line
    overflow_tolerance: float = 0.4   # playback may overrun the free time by this
    stretch_max: float = 1.0          # slowest picture over a caption window (1.0 = never)


@dataclass(slots=True)
class RowPlan:
    sub_id: int
    source_start: float
    source_end: float
    place_start: float
    playback_duration: float
    tempo: float
    early_lead: float       # source_start - place_start (positive = early)
    free_after: float       # seconds until the next caption's picture (-1 = last row)
    overflow_seconds: float  # playback overruns free_after + tolerance
    gap_before: float       # pause before this clip (the source's own rhythm)
    stretch: float = 1.0    # picture slow-down over this row's window (1.0 = none)
    stretch_seconds: float = 0.0  # output seconds the stretch added


def plan_rows(
    rows: list[SpeechRow],
    durations: dict[int, float],
    config: TimelineConfig | None = None,
) -> list[RowPlan]:
    """Place each row's clip at its caption window (see module docstring)."""
    return plan_rows_with_warp(rows, durations, config)[0]


def plan_rows_with_warp(
    rows: list[SpeechRow],
    durations: dict[int, float],
    config: TimelineConfig | None = None,
) -> tuple[list[RowPlan], TimeWarp]:
    """:func:`plan_rows` plus the picture stretch the placements assume."""
    config = config or TimelineConfig()
    plans: list[RowPlan] = []
    segments: list[WarpSegment] = []
    warp = TimeWarp()
    shift = 0.0   # output seconds the stretches before this row added
    cursor = 0.0  # output time
    for index, row in enumerate(rows):
        raw = max(0.001, durations.get(row.sub_id, 0.0))
        # No inter-clip gap is added: back-to-back captions are back-to-back dialogue,
        # and an extra 0.08s per row accumulated a measured 9s of drift over a video.
        # The assembly crossfade declicks touching clips.
        start = max(cursor, row.anchor_start + shift - config.lead)
        next_start = rows[index + 1].anchor_start if index + 1 < len(rows) else None
        free = float(next_start + shift - start) if next_start is not None else -1.0
        tempo = 1.0
        if 0.05 < free < raw:
            tempo = min(config.tempo_ceiling, raw / free)
        duration = raw / tempo
        stretch = extra = 0.0
        if config.stretch_max > 1.0 and next_start is not None and free > 0.05 and duration > free:
            window = next_start - row.anchor_start
            # windows must not overlap (out-of-order captions): the warp maps assume it
            disjoint = not segments or row.anchor_start >= segments[-1].start + segments[-1].length
            extra = min(duration - free, window * (config.stretch_max - 1.0)) if window > 0.05 and disjoint else 0.0
            if extra >= 0.02:
                segment = WarpSegment(row.anchor_start, window, 1.0 + extra / window)
                segments.append(segment)
                warp = TimeWarp(segments)
                stretch, extra = segment.factor, segment.extra
                shift += extra
                free += extra
            else:
                extra = 0.0
        place = warp.to_source(start) if segments else start
        overflow = 0.0 if free < 0 else max(0.0, duration - free - config.overflow_tolerance)
        plans.append(
            RowPlan(
                sub_id=row.sub_id,
                source_start=round(row.anchor_start, 3),
                source_end=round(row.anchor_end, 3),
                place_start=round(place, 3),
                playback_duration=round(duration, 3),
                tempo=round(tempo, 4),
                early_lead=round(row.anchor_start - place, 3),
                free_after=round(free, 3),
                overflow_seconds=round(overflow, 3),
                gap_before=round(max(0.0, start - cursor), 3),
                stretch=round(stretch or 1.0, 4),
                stretch_seconds=round(extra, 3),
            )
        )
        cursor = start + duration
    return plans, warp


def summarize(plans: list[RowPlan], warp: TimeWarp | None = None) -> dict:
    """Measured placement health: lead bounds, pauses, overflows, tempo use."""
    if not plans:
        return {"rows": 0}
    early = max((p.early_lead for p in plans), default=0.0)
    late = max((-p.early_lead for p in plans), default=0.0)
    pauses = sum(p.gap_before for p in plans if p.gap_before > 0.15)
    overflows = [p for p in plans if p.overflow_seconds > 0.0]
    return {
        "rows": len(plans),
        "max_early_lead_s": round(early, 3),
        "max_late_lead_s": round(late, 3),
        "pause_seconds": round(pauses, 2),
        "overflow_rows": len(overflows),
        "overflow_seconds": round(sum(p.overflow_seconds for p in overflows), 2),
        "max_tempo": max((p.tempo for p in plans), default=1.0),
        "stretch_rows": sum(1 for p in plans if p.stretch > 1.0),
        "stretch_seconds": round(sum(p.stretch_seconds for p in plans), 2),
        "last_voice_end": round((warp.to_output(plans[-1].place_start) if warp else plans[-1].place_start) + plans[-1].playback_duration, 3),
    }
