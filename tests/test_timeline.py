from backend.app.dubbing.models import Unit
from backend.app.dubbing.timeline import TimelineConfig, build_runs, plan_run, plan_timeline, summarize


def _units(spans: list[tuple[int, float, float]]) -> list[Unit]:
    return [Unit(id=i, segment_ids=f"{i}-{i}", start=s, end=e, source_zh=f"u{i}") for i, (s, e) in enumerate(spans)]


def test_build_runs_splits_only_on_real_pauses():
    units = _units([(0.0, 2.0), (2.2, 4.0), (4.9, 6.0), (8.0, 9.0)])
    runs = build_runs(units, TimelineConfig(run_gap=0.8))
    assert [(r.start, r.end, len(r.unit_ids)) for r in runs] == [(0.0, 4.0, 2), (4.9, 6.0, 1), (8.0, 9.0, 1)]


def test_sentence_boundary_gets_a_floor_even_when_sources_touch():
    # two sentences split inside one unit: block gap is 0, but the period must buy a pause
    units = _units([(0.0, 5.0), (5.0, 10.0)])
    plan = plan_run(build_runs(units)[0], units, {0: 4.0, 1: 4.0}, TimelineConfig(), min_breaths={1: 0.3})
    assert plan.placements[1].breath_before >= 0.3 - 1e-6


def test_source_pause_is_kept_as_rhythm():
    # a 0.6s source pause between two units must survive as a real breath
    units = _units([(0.0, 2.0), (2.6, 4.6)])
    plan = plan_run(build_runs(units)[0], units, {0: 1.8, 1: 1.8}, TimelineConfig())
    assert 0.35 <= plan.placements[1].breath_before <= 0.45


def test_short_dub_fills_with_breaths_within_lead_budget():
    # span 10s, voice 8s (0.8x) -> tempo floor 0.92 cannot fill; breaths spend the rest
    units = _units([(0.0, 5.0), (5.0, 10.0)])
    plan = plan_run(build_runs(units)[0], units, {0: 4.0, 1: 4.0}, TimelineConfig())
    assert plan.tempo == 0.92
    assert plan.fill_ratio > 0.85
    # contiguous speech gets only the minimum breath; the remaining slack is
    # trailing (before the next real pause) and flagged for the fit loop
    assert plan.trailing_silence <= 1.5
    assert plan.needs_expand is True
    assert all(p.early_lead <= TimelineConfig().max_lead + 1e-6 for p in plan.placements)
    assert 0.06 <= plan.placements[1].breath_before <= 0.25


def test_long_dub_speeds_up_then_overflows_locally():
    # span 6s, voice 8s -> speed up to ceiling, remainder overflows into the pause
    units = _units([(0.0, 3.0), (3.0, 6.0)])
    plan = plan_run(build_runs(units)[0], units, {0: 4.0, 1: 4.0}, TimelineConfig())
    assert plan.tempo == TimelineConfig().tempo_ceiling
    assert plan.overflow_seconds > 0
    assert plan.fill_ratio > 1.0


def test_underfilled_run_is_flagged_for_expand():
    units = _units([(0.0, 10.0)])
    plan = plan_run(build_runs(units)[0], units, {0: 3.0}, TimelineConfig())
    assert plan.needs_expand is True
    plan_ok = plan_run(build_runs(units)[0], units, {0: 9.8}, TimelineConfig())
    assert plan_ok.needs_expand is False
    assert plan_ok.trailing_silence <= 1.5


def test_chain_mode_places_monotonically_with_bounded_breaths():
    # five short clips in one long run: chained with breaths, no per-unit re-anchor
    units = _units([(i * 4.0, i * 4.0 + 4.0) for i in range(5)])
    durations = {i: 1.5 for i in range(5)}
    plan = plan_run(build_runs(units)[0], units, durations, TimelineConfig())
    assert plan.placements[0].place_start == 0.0
    gaps = [p.breath_before for p in plan.placements[1:]]
    assert all(0.06 - 1e-9 <= g <= 0.25 + 1e-9 for g in gaps)
    ends = [p.place_start + p.playback_duration for p in plan.placements]
    starts = [p.place_start for p in plan.placements]
    assert all(starts[i + 1] >= ends[i] - 1e-6 for i in range(len(ends) - 1))


def test_lead_budget_holds_short_voice_back_to_the_picture():
    # three short clips in one run: without the budget the voice would pack onto the
    # start and end ~5.5s early, leaving the run's tail silent (no dub, no subtitles)
    units = _units([(0.0, 3.0), (3.05, 6.05), (6.1, 9.1)])
    plan = plan_run(build_runs(units)[0], units, {0: 1.0, 1: 1.0, 2: 1.0}, TimelineConfig())
    assert plan.tempo == TimelineConfig().tempo_floor
    assert all(p.early_lead <= TimelineConfig().max_lead + 1e-6 for p in plan.placements)
    assert plan.hold_seconds > 1.0
    assert plan.placements[-1].place_start >= 6.1 - TimelineConfig().max_lead - 1e-6


def test_summary_reports_metrics_sample():
    units = _units([(0.0, 5.0), (5.0, 10.0)])
    plans = plan_timeline(units, {0: 4.0, 1: 4.0})
    summary = summarize(plans)
    assert summary["runs"] == 1
    assert summary["max_early_lead_s"] <= TimelineConfig().max_lead
