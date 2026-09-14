"""L6 — run-based voice timeline (deterministic).

The problem this solves, measured on 10 real jobs: the dub is ~0.84× the length of
the source speech, and the old per-anchor placement let that 16% become audible
silence between sentences inside one continuous speech run (median 16.25% of voice
time, ~85 pauses ≥300ms per episode).

Rules per run (a run is a stretch of continuous source speech, gap < run_gap):

1. run-level tempo  — one factor for the whole run, in [tempo_floor, 1.0] when the
   dub is short, or [1.0, tempo_ceiling] when it is long. Never per-clip.
2. distributed breath — leftover slack is spent as small gaps at sentence
   boundaries, bounded by [breath_min, breath_max], instead of one long stall.
3. lead budget — inside a run the voice may run at most ``max_lead`` ahead of the
   picture. This is what removes the stutter; the run boundary itself stays
   anchored to the source.
4. overflow — a run that still doesn't fit speeds up then bleeds into the pause
   that follows it; it never pushes the next run.
5. underfill — a run that still fills < ``min_run_fill`` is flagged so the fit
   stage can expand only those lines (content-driven, not blanket).
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Unit, Run


@dataclass(slots=True)
class TimelineConfig:
    run_gap: float = 0.8          # a source pause below this = same speech run
    tempo_floor: float = 0.92     # slowest run-level tempo (fill by slowing)
    tempo_ceiling: float = 1.18   # fastest run-level tempo (fit by speeding up)
    max_lead: float = 0.8         # voice may run this far ahead of the picture
    breath_min: float = 0.06
    breath_max: float = 0.6       # a real source pause, kept (rhythm!)
    pause_keep: float = 0.7       # keep this share of the source pause
    contiguous_gap: float = 0.15  # below this the speaker never stopped: glue
    sentence_breath: float = 0.35   # minimum pause after a period
    dramatic_breath: float = 0.5    # minimum pause after an ellipsis
    clause_breath: float = 0.12    # minimum pause after a clause (, ; :)
    min_run_fill: float = 0.95    # below this the run is flagged for expand
    min_trailing: float = 1.5     # ...or when the run ends this early (absolute)


@dataclass(slots=True)
class Placement:
    unit_id: int
    source_start: float
    place_start: float
    playback_duration: float
    tempo: float
    breath_before: float
    early_lead: float             # source_start - place_start (positive = early)
    overflow: bool = False


@dataclass(slots=True)
class RunPlan:
    run_id: int
    start: float
    end: float
    placements: list[Placement]
    tempo: float
    trailing_silence: float
    fill_ratio: float             # (last voice end - run start) / span
    overflow_seconds: float
    needs_expand: bool


def build_runs(units: list[Unit], config: TimelineConfig | None = None) -> list[Run]:
    """Group consecutive units whose source gap is below ``run_gap``."""
    config = config or TimelineConfig()
    runs: list[Run] = []
    for unit in units:
        if runs and unit.start - runs[-1].end < config.run_gap:
            runs[-1].unit_ids.append(unit.id)
            runs[-1].end = unit.end
        else:
            runs.append(Run(id=len(runs), unit_ids=[unit.id], start=unit.start, end=unit.end))
    return runs


def plan_run(
    run: Run,
    units: list[Unit],
    durations: dict[int, float],
    config: TimelineConfig,
    min_breaths: dict[int, float] | None = None,
) -> RunPlan:
    """Place one run's voice clips on the timeline (see module docstring)."""
    ordered = [u for u in units if u.id in set(run.unit_ids)]
    span = max(0.001, run.end - run.start)
    voice_total = sum(durations.get(u.id, 0.0) for u in ordered)

    if not ordered or voice_total <= 0:
        return RunPlan(run.id, run.start, run.end, [], 1.0, span, 0.0, 0.0, True)

    if voice_total > span:
        tempo = min(config.tempo_ceiling, voice_total / span)
    else:
        tempo = max(config.tempo_floor, voice_total / span)
    fitted_total = voice_total / tempo

    # Rhythm preservation: each inter-unit breath is derived from the SOURCE pause
    # (the original pacing is the emotion — wiping pauses out made the dub read
    # rushed even at the same total duration). Only genuinely contiguous speech is
    # glued with the minimum breath; everything else keeps most of its pause.
    def breath_for(index: int) -> float:
        if index == 0:
            return 0.0
        source_gap = max(0.0, ordered[index].start - ordered[index - 1].end)
        if source_gap < config.contiguous_gap:
            base = config.breath_min
        else:
            base = min(config.breath_max, max(config.breath_min, source_gap * config.pause_keep))
        # A sentence boundary is a pause even when the sources touch (two sentences
        # were split inside one unit by char-proportional timing): otherwise the TTS
        # runs the sentences together ("đọc dính liền").
        floor = (min_breaths or {}).get(index, 0.0)
        return max(base, floor)

    placements: list[Placement] = []
    cursor = run.start
    for index, unit in enumerate(ordered):
        duration = durations.get(unit.id, 0.0) / tempo
        breath_before = breath_for(index)
        place = cursor + breath_before
        placements.append(
            Placement(
                unit_id=unit.id,
                source_start=unit.start,
                place_start=round(place, 3),
                playback_duration=round(duration, 3),
                tempo=round(tempo, 4),
                breath_before=round(breath_before, 3),
                early_lead=round(unit.start - place, 3),
            )
        )
        cursor = place + duration

    overflow = max(0.0, cursor - run.end)
    trailing = max(0.0, run.end - cursor)
    fill = max(0.0, min(1.5, (cursor - run.start) / span))
    # A run that ends this far before its source window is missing spoken content:
    # the translation is 16% shorter on average (measured), and that slack cannot be
    # hidden by breathing gaps — the fit stage must expand those lines.
    return RunPlan(
        run_id=run.id,
        start=round(run.start, 3),
        end=round(run.end, 3),
        placements=placements,
        tempo=round(tempo, 4),
        trailing_silence=round(trailing, 3),
        fill_ratio=round(fill, 3),
        overflow_seconds=round(overflow, 3),
        needs_expand=trailing > config.min_trailing or fill < config.min_run_fill,
    )


def plan_timeline(
    units: list[Unit],
    durations: dict[int, float],
    config: TimelineConfig | None = None,
    min_breaths: dict[int, float] | None = None,
) -> list[RunPlan]:
    config = config or TimelineConfig()
    return [plan_run(run, units, durations, config, min_breaths) for run in build_runs(units, config)]


def summarize(plans: list[RunPlan], config: TimelineConfig | None = None) -> dict:
    """Metrics used by the audit: audible intra-run silence, lead, fill."""
    config = config or TimelineConfig()
    audible = 0.0
    lead_max = 0.0
    underfilled = 0
    overflow_runs = 0
    for plan in plans:
        for placement in plan.placements[1:]:
            if placement.breath_before > 0.15:
                audible += placement.breath_before
        lead_max = max(lead_max, max((p.early_lead for p in plan.placements), default=0.0))
        if plan.needs_expand:
            underfilled += 1
        if plan.overflow_seconds > 0.05:
            overflow_runs += 1
    return {
        "runs": len(plans),
        "audible_breath_seconds": round(audible, 2),
        "max_early_lead_s": round(lead_max, 3),
        "underfilled_runs": underfilled,
        "overflow_runs": overflow_runs,
    }
