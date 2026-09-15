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
