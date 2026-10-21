from backend.app.dubbing.models import SpeechRow
from backend.app.dubbing.timeline import TimelineConfig, plan_rows, summarize


def _rows(spans: list[tuple[float, float]]) -> list[SpeechRow]:
    return [SpeechRow(i, 0, s, e, f"{i}-{i}", f"line {i}") for i, (s, e) in enumerate(spans)]


def test_rows_anchor_to_their_own_caption():
    # four 2s captions 3s apart; each voice is 1s -> every clip starts at its caption
    rows = _rows([(0.0, 2.0), (3.0, 5.0), (6.0, 8.0), (9.0, 11.0)])
    plans = plan_rows(rows, {i: 1.0 for i in range(4)}, TimelineConfig(lead=0.0))
    assert [p.place_start for p in plans] == [0.0, 3.0, 6.0, 9.0]
    assert all(p.tempo == 1.0 for p in plans)
    assert all(p.early_lead == 0.0 for p in plans)


def test_short_voice_does_not_drift_ahead_across_rows():
    # the old run-packing model packed these onto 0-4s and left the tail silent;
    # per-row re-anchoring can never end up more than `lead` ahead of the picture
    rows = _rows([(0.0, 2.0), (10.0, 12.0), (20.0, 22.0), (30.0, 32.0)])
    plans = plan_rows(rows, {i: 0.5 for i in range(4)}, TimelineConfig(lead=0.0))
    assert [p.place_start for p in plans] == [0.0, 10.0, 20.0, 30.0]
    assert max(p.early_lead for p in plans) <= 1e-6
    assert max(-p.early_lead for p in plans) <= 1e-6


def test_long_voice_speeds_up_only_to_fit_the_free_time():
    # caption 0-2s, next caption at 3s: free 3s, raw 3.6s -> ceil(3.6/3)=1.18
    rows = _rows([(0.0, 2.0), (3.0, 4.0)])
    plans = plan_rows(rows, {0: 3.6, 1: 0.5}, TimelineConfig(tempo_ceiling=1.18))
    assert plans[0].tempo == 1.18
    assert plans[0].overflow_seconds == 0.0  # 3.6/1.18 = 3.05 <= free 3.0 + tolerance
    # a line that cannot fit even at the ceiling is reported, not hidden
    plans = plan_rows(rows, {0: 6.0, 1: 0.5}, TimelineConfig(tempo_ceiling=1.18))
    assert plans[0].tempo == 1.18
    assert plans[0].overflow_seconds > 1.0


def test_late_predecessor_re_anchors_at_the_next_picture():
    # row 0 overflows into row 1's picture; row 1 starts late, but row 2 (whose
    # predecessor finished early) is back on its own caption
    config = TimelineConfig()
    rows = _rows([(0.0, 1.0), (1.5, 2.5), (6.0, 7.0)])
    plans = plan_rows(rows, {0: 2.0, 1: 0.5, 2: 0.5}, config)
    assert plans[0].place_start == 0.0
    end0 = plans[0].place_start + plans[0].playback_duration
    assert abs(plans[1].place_start - end0) < 1e-6    # pushed by the overflow, no added gap
    assert plans[1].early_lead < 0                    # late, and reported as such
    assert abs(plans[2].place_start - (6.0 - config.lead)) < 1e-6    # re-anchored to its caption
    assert abs(plans[2].early_lead - config.lead) < 1e-6


def test_summarize_reports_leads_pauses_and_overflows():
    rows = _rows([(0.0, 2.0), (3.0, 5.0), (6.0, 8.0), (12.0, 14.0)])
    plans = plan_rows(rows, {0: 1.0, 1: 4.5, 2: 1.0, 3: 1.0}, TimelineConfig())
    summary = summarize(plans)
    assert summary["rows"] == 4
    assert summary["max_early_lead_s"] <= TimelineConfig().lead + 1e-6
    assert summary["max_late_lead_s"] > 0
    assert summary["overflow_rows"] >= 1
    assert summary["pause_seconds"] > 0


def _reference_plan(rows, durations, config):
    """The planner before the video stretch existed (field-by-field regression oracle)."""
    out, cursor = [], 0.0
    for index, row in enumerate(rows):
        raw = max(0.001, durations.get(row.sub_id, 0.0))
        start = max(cursor, row.anchor_start - config.lead)
        next_start = rows[index + 1].anchor_start if index + 1 < len(rows) else None
        free = float(next_start - start) if next_start is not None else -1.0
        tempo = min(config.tempo_ceiling, raw / free) if 0.05 < free < raw else 1.0
        duration = raw / tempo
        overflow = 0.0 if free < 0 else max(0.0, duration - free - config.overflow_tolerance)
        out.append((round(start, 3), round(duration, 3), round(tempo, 4), round(row.anchor_start - start, 3),
                    round(free, 3), round(overflow, 3), round(max(0.0, start - cursor), 3)))
        cursor = start + duration
    return out


def test_stretch_off_is_the_old_planner_exactly():
    import random

    from backend.app.dubbing.timeline import plan_rows_with_warp

    rng = random.Random(11)
    spans, t = [], 0.0
    for _ in range(300):
        t += rng.uniform(0.0, 2.0)
        length = rng.uniform(0.3, 4.0)
        spans.append((t, t + length))
        t += length
    rows = _rows(spans)
    durations = {i: rng.uniform(0.2, 6.0) for i in range(len(rows))}
    config = TimelineConfig()
    plans, warp = plan_rows_with_warp(rows, durations, config)
    assert not warp
    got = [(p.place_start, p.playback_duration, p.tempo, p.early_lead, p.free_after, p.overflow_seconds, p.gap_before)
           for p in plans]
    assert got == _reference_plan(rows, durations, config)
    assert all(p.stretch == 1.0 and p.stretch_seconds == 0.0 for p in plans)


def _stretch_config(**kw):
    return TimelineConfig(tempo_ceiling=1.10, stretch_max=1.111, **kw)


def test_stretch_slows_the_window_then_reports_the_rest_as_overflow():
    from backend.app.dubbing.timeline import plan_rows_with_warp

    # free 3.0s, raw 4.2s -> 3.818s at 1.10x; the 3s window can add at most 0.333s
    rows = _rows([(0.0, 2.0), (3.0, 4.0)])
    plans, warp = plan_rows_with_warp(rows, {0: 4.2, 1: 0.5}, _stretch_config())
    assert plans[0].tempo == 1.1
    assert plans[0].stretch == 1.111
    assert abs(plans[0].stretch_seconds - 0.333) < 1e-3
    assert abs(plans[0].free_after - 3.333) < 1e-3
    assert abs(plans[0].overflow_seconds - (4.2 / 1.1 - 3.333 - 0.4)) < 2e-3
    assert len(warp.segments) == 1 and warp.segments[0].start == 0.0 and warp.segments[0].length == 3.0
    # row 1 follows row 0's voice (output 3.818s), mapped back to source time
    assert abs(warp.to_output(plans[1].place_start) - 4.2 / 1.1) < 2e-3


def test_later_rows_keep_their_source_place_and_shift_in_output():
    from backend.app.dubbing.timeline import plan_rows_with_warp

    config = _stretch_config()
    rows = _rows([(0.0, 2.0), (3.0, 4.0), (6.0, 7.0)])
    plans, warp = plan_rows_with_warp(rows, {0: 3.6, 1: 0.5, 2: 0.5}, config)
    extra = 3.6 / 1.1 - 3.0  # fully absorbed by the stretch: no overflow, no LLM
    assert plans[0].overflow_seconds == 0.0
    assert abs(plans[0].stretch_seconds - extra) < 1e-3
    assert abs(plans[1].place_start - 3.0) < 1e-3  # right on its own caption
    assert abs(plans[2].place_start - (6.0 - config.lead)) < 1e-3
    assert abs(warp.to_output(plans[2].place_start) - (6.0 - config.lead + extra)) < 1e-3
    summary = summarize(plans)
    assert summary["stretch_rows"] == 1
    assert abs(summary["stretch_seconds"] - extra) < 0.01


def test_last_row_is_never_stretched():
    from backend.app.dubbing.timeline import plan_rows_with_warp

    rows = _rows([(0.0, 2.0), (3.0, 4.0)])
    plans, warp = plan_rows_with_warp(rows, {0: 0.5, 1: 9.0}, _stretch_config())
    assert not warp
    assert plans[1].stretch == 1.0 and plans[1].free_after == -1.0
