import shutil
import subprocess

import pytest

import backend.app.dubbing.repair as repair_mod
from backend.app.agents import AgentConfig
from backend.app.dubbing.models import SpeechRow
from backend.app.dubbing.render import RenderConfig, _blocks_from_rows
from backend.app.dubbing.repair import RepairConfig, compute_targets, repair

LLM = AgentConfig()


def test_blocks_from_rows_is_one_clip_per_caption():
    rows = [
        SpeechRow(0, 0, 0.0, 3.0, "0-0", "Câu một ⟂ Câu hai"),
        SpeechRow(1, 1, 3.0, 6.0, "1-1", "Câu ba"),
    ]
    blocks, usable = _blocks_from_rows(rows, text_mode="continuous")
    # one block per row; the turn marker is stripped but the line stays whole
    assert [b.block_id for b in blocks] == ["0", "1"]
    assert "⟂" not in blocks[0].text
    assert "Câu một" in blocks[0].text and "Câu hai" in blocks[0].text
    assert blocks[0].start == 0.0 and blocks[0].end == 3.0
    assert [r.sub_id for r in usable] == [0, 1]


def test_compute_targets_shrinks_overbudget_row():
    row = SpeechRow(0, 5, 0.0, 2.0, "10-10", "Một câu khá dài cần rút gọn lại nhiều")
    # the free time is 2.0s but the measured voice took 3.0s
    targets = compute_targets({0: row}, {0: (2.0, 3.0)}, RepairConfig(target_scale=0.9))
    assert len(targets) == 1
    assert targets[0].sub_id == 0
    assert targets[0].target_chars < len(row.text)


def test_repair_accepts_shorter_rewrite_rejects_longer(monkeypatch):
    rows = [SpeechRow(0, 5, 0.0, 2.0, "10-10", "Một câu rất dài lê thê cần được rút gọn đi nhiều lắm")]

    # rewrite is shorter -> accepted
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\tCâu ngắn")
    out, n = repair(rows, {0: "源"}, [], {0: (2.0, 3.2)}, LLM, RepairConfig())
    assert n == 1
    assert out[0].text == "Câu ngắn"
    assert [r.sub_id for r in out] == list(range(len(out)))

    # rewrite is not shorter -> rejected, original kept
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\tMột câu còn dài hơn cả bản gốc lê thê dài dòng thêm nữa")
    out, n = repair(rows, {0: "源"}, [], {0: (2.0, 3.2)}, LLM, RepairConfig())
    assert n == 0
    assert out[0].text.startswith("Một câu rất dài")


def test_repair_rejects_han(monkeypatch):
    rows = [SpeechRow(0, 5, 0.0, 2.0, "10-10", "Một câu dài cần rút gọn")]
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\t还有中文")
    out, n = repair(rows, {0: "源"}, [], {0: (2.0, 3.0)}, LLM, RepairConfig())
    assert n == 0


def test_render_config_defaults():
    cfg = RenderConfig(provider_url="http://x")
    assert cfg.speaker == "BV421_vivn_streaming"
    assert cfg.tempo_ceiling == 1.18
    assert cfg.overflow_tolerance == 0.4


def test_expand_completes_underfilled_keeps_complete(monkeypatch):
    from backend.app.dubbing.repair import expand

    rows = [SpeechRow(0, 5, 0.0, 6.0, "10-10", "Lời ngắn")]

    # LLM restores dropped content (much longer) -> accepted
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\tLời đầy đủ hơn nhiều với toàn bộ ý của câu gốc được khôi phục lại")
    out, n = expand(rows, {0: "源"}, [], {0: (6.0, 2.0)}, LLM, RepairConfig())
    assert n == 1
    assert "đầy đủ" in out[0].text

    # LLM judges it complete and returns roughly the same -> kept unchanged
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\tLời ngắn")
    out, n = expand(rows, {0: "源"}, [], {0: (6.0, 2.0)}, LLM, RepairConfig())
    assert n == 0
    assert out[0].text == "Lời ngắn"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_render_places_clips_in_the_stretched_output_time(tmp_path):
    """A stretched row shifts every LATER clip by the stretch in the voice track, while
    the timings (fit loop, subtitles) stay in source time."""
    from backend.app.dubbing.render import render

    def tone(seconds):
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-f", "wav", "-"],
            check=True, capture_output=True,
        ).stdout

    class Engine:
        cache_id = "fake:stretch"
        voice = "v"
        clips = {"dài": tone(3.6), "ngắn": tone(0.5), "ba": tone(0.5)}

        def synthesize_batch(self, texts):
            return [self.clips[t] for t in texts]

    rows = [SpeechRow(0, 0, 0.0, 2.0, "0-0", "dài"), SpeechRow(1, 1, 3.0, 4.0, "1-1", "ngắn"),
            SpeechRow(2, 2, 6.0, 7.0, "2-2", "ba")]
    config = RenderConfig(provider_url="x", workers=2, tempo_ceiling=1.10, stretch_max=1.111, text_mode="continuous")
    result = render(rows, config, tmp_path / "work", tmp_path / "cache", engine=Engine())
    extra = result.warp.extra_seconds()
    assert len(result.warp.segments) == 1 and 0.2 < extra < 0.34
    assert result.timings[0].stretch > 1.0 and result.timings[0].overflow_ms == 0
    assert abs(result.timings[2].place_start - (6.0 - config.lead)) < 1e-3  # source time
    assert abs(result.placements[2][0] - (result.timings[2].place_start + extra)) < 2e-3  # output time
    assert abs(result.total_duration - (7.0 + extra)) < 1e-6
    assert result.summary["timeline"]["stretch_rows"] == 1

    off = render(rows, RenderConfig(provider_url="x", workers=2, text_mode="continuous"),
                 tmp_path / "work", tmp_path / "cache", engine=Engine())
    assert not off.warp
    assert [p[0] for p in off.placements] == [t.place_start for t in off.timings]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_a_skipped_empty_row_does_not_shift_the_durations(tmp_path):
    """The planner looks durations up by row id: a row whose text cleans to nothing is
    skipped, and keying by list position handed every later row its neighbour's clip."""
    from backend.app.dubbing.render import render

    def tone(seconds):
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-f", "wav", "-"],
            check=True, capture_output=True,
        ).stdout

    class Engine:
        cache_id = "fake:skip"
        voice = "v"
        clips = {"một": tone(0.6), "hai": tone(2.4)}

        def synthesize_batch(self, texts):
            return [self.clips[t] for t in texts]

    rows = [SpeechRow(0, 0, 0.0, 2.0, "0-0", "một"), SpeechRow(1, 1, 3.0, 4.0, "1-1", ""),
            SpeechRow(2, 2, 6.0, 9.0, "2-2", "hai")]
    result = render(rows, RenderConfig(provider_url="x", workers=2, text_mode="continuous"),
                    tmp_path / "work", tmp_path / "cache", engine=Engine())
    assert [p.sub_id for p in result.plans] == [0, 2]
    for plan, timing in zip(result.plans, result.timings):
        assert abs(plan.playback_duration - timing.tts_duration) < 0.05
