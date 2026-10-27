import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import backend.app.services.hardsub as hardsub
from backend.app.infrastructure.packaging import package_outputs
from backend.app.services.hardsub import HardsubConfig, _build_cover_chain


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.parametrize("mode", ["blur", "darken", "solid", "frost", "pixelate"])
@pytest.mark.parametrize("height", [2, 3, 4, 40, 41, 74, 80, 81])
def test_a_cover_box_of_any_height_renders(height, mode):
    # an 80px subtitle band used to ask for chroma radius 20 and kill the whole render
    chain = _build_cover_chain([(0, 900, 1920, height, mode, 1.0, "#123456")], HardsubConfig(blur=20))
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=1920x1080", "-frames:v", "1",
         "-filter_complex", f"[0:v]format=yuv420p[src];{chain.replace('[0:v]', '[src]', 1)}[cov]null[out]",
         "-map", "[out]", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_a_failed_render_fails_the_package_instead_of_passing_without_a_video(monkeypatch, tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("Command failed (234): ffmpeg ... boxblur")

    monkeypatch.setattr(hardsub, "build_hardsub", broken)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    data = tmp_path / "data"
    (data / "subtitles").mkdir(parents=True)
    subtitle = data / "subtitles" / "ep.vi.display.json"
    subtitle.write_text('[{"start": 0.0, "end": 1.0, "text": "Xin chào"}]')
    voice = data / "voice.m4a"
    voice.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="boxblur"):
        package_outputs(Path("v.mp4"), subtitle, data / "ep.srt", data / "packages", data, voice_track=voice)


def _captured_hardsub(monkeypatch, tmp_path, warp, config=None, gap=False):
    """Run build_hardsub with ffmpeg/detection stubbed out; return the final command."""
    calls = []
    monkeypatch.setattr(hardsub, "run_ffmpeg_progress", lambda args, total, report=None, cancel=None: calls.append(args) or [])
    monkeypatch.setattr(hardsub, "detect_sub_band", lambda video, cfg: None)
    monkeypatch.setattr(hardsub, "_build_overlay_video", lambda *args, **kwargs: None)
    import backend.app.services.audio as audio

    monkeypatch.setattr(audio, "warp_audio", lambda video, w, out, work: out)
    monkeypatch.setattr(audio, "render_gap_gain", lambda *args, **kwargs: {"gaps": 1, "seconds": 2.0})
    voice = tmp_path / "voice.m4a"
    voice.write_bytes(b"x")
    config = config or HardsubConfig(sharpen=0.0, gap_volume=0.5 if gap else 0.0)
    hardsub.build_hardsub(tmp_path / "v.mp4", [], voice, tmp_path / "out.mp4", tmp_path / "work", config, warp=warp)
    command = calls[-1]
    inputs = [command[i + 1] for i, arg in enumerate(command) if arg == "-i"]
    return inputs, command[command.index("-filter_complex") + 1]


def test_without_a_stretch_the_render_command_is_unchanged(monkeypatch, tmp_path):
    from backend.app.dubbing.time_warp import TimeWarp

    for warp in (None, TimeWarp()):
        inputs, graph = _captured_hardsub(monkeypatch, tmp_path, warp, gap=True)
        assert [Path(p).name for p in inputs] == ["v.mp4", "vi_overlay.mov", "voice.m4a", "bed_gain.wav"]
        assert graph.startswith("[0:v]null[scaled];")
        assert "setpts" not in graph and "fps=" not in graph
        assert "[0:a:0]aresample" in graph and "[2:a:0]aresample" in graph and "[3:a:0]aresample" in graph


def test_a_stretch_retimes_the_picture_and_swaps_in_the_stretched_bed(monkeypatch, tmp_path):
    from backend.app.dubbing.time_warp import TimeWarp, WarpSegment

    warp = TimeWarp([WarpSegment(1.0, 2.0, 1.1)])
    inputs, graph = _captured_hardsub(monkeypatch, tmp_path, warp, gap=True)
    assert [Path(p).name for p in inputs] == ["v.mp4", "vi_overlay.mov", "voice.m4a", "bed_warped.flac", "bed_gain.wav"]
    assert graph.startswith(f"[0:v]{warp.setpts_filter()},fps=30[scaled];")  # source fps (none probed: 30)
    assert str(hardsub.Fraction(30000 / 1001).limit_denominator(1001)) == "30000/1001"  # NTSC stays exact
    assert "[0:a:0]" not in graph
    assert "[3:a:0]aresample" in graph and "[2:a:0]aresample" in graph and "[4:a:0]aresample" in graph

    # the canonical scale filter already ends in fps=: no second one
    config = HardsubConfig(sharpen=0.0, scale_filter="scale=1920:1080,fps=30")
    _inputs, graph = _captured_hardsub(monkeypatch, tmp_path, warp, config=config)
    assert graph.startswith(f"[0:v]{warp.setpts_filter()},scale=1920:1080,fps=30[scaled];")


def test_package_maps_cues_into_the_stretched_time(monkeypatch, tmp_path):
    import json

    from backend.app.dubbing.time_warp import TimeWarp, WarpSegment

    seen = {}

    def fake_build(video, cues, voice, output, work, config, warp=None, cancel=None, report=None):
        seen["cues"], seen["warp"], seen["cancel"] = cues, warp, cancel
        return {}

    monkeypatch.setattr(hardsub, "build_hardsub", fake_build)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    data = tmp_path / "data"
    (data / "subtitles").mkdir(parents=True)
    subtitle = data / "subtitles" / "ep.vi.display.json"
    subtitle.write_text('[{"start": 0.5, "end": 1.5, "text": "Một"}, {"start": 4.0, "end": 5.0, "text": "Hai"}]')
    voice = data / "voice.m4a"
    voice.write_bytes(b"x")
    warp = TimeWarp([WarpSegment(1.0, 2.0, 1.25)])  # +0.5s
    stop = threading.Event()
    package_outputs(Path("v.mp4"), subtitle, data / "ep.srt", data / "packages", data, voice_track=voice, warp=warp, cancel=stop)
    assert [(c["start"], c["end"]) for c in seen["cues"]] == [(0.5, 1.625), (4.5, 5.5)]
    assert seen["warp"] is warp
    assert seen["cancel"] is stop  # the Stop button reaches the render
    assert json.loads(subtitle.read_text())[1]["start"] == 4.0  # the editor's file stays in source time
    assert "00:00:04.500" in (data / "packages" / "ep.vi.display.vtt").read_text()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_a_stretched_render_runs_as_long_as_the_warp_says(tmp_path):
    from backend.app.dubbing.time_warp import TimeWarp, WarpSegment
    from backend.app.services.audio import probe_duration

    video = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=4",
         "-f", "lavfi", "-i", "sine=f=440:duration=4", "-pix_fmt", "yuv420p", "-shortest", str(video)],
        check=True,
    )
    warp = TimeWarp([WarpSegment(1.0, 1.5, 1.111), WarpSegment(3.0, 0.8, 1.1)])
    total = warp.to_output(4.0)
    voice = tmp_path / "voice.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f=220:duration={total}", str(voice)], check=True)
    out = tmp_path / "out.mp4"
    cues = [{"start": warp.to_output(0.5), "end": warp.to_output(3.5), "text": "Xin chào"}]
    # duck=False: the sidechain compressor drops a random 0.1-0.9s off the END of the
    # mix (with or without a stretch), which would make the length check flaky
    hardsub.build_hardsub(video, cues, voice, out, tmp_path / "work", HardsubConfig(duck=False), warp=warp)

    def stream_duration(kind):
        return float(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries", "stream=duration", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=True,
        ).stdout.strip())

    assert abs(stream_duration("v:0") - total) <= 1.5 / 30
    assert abs(stream_duration("a:0") - total) <= 0.05
    assert abs(probe_duration(out) - total) <= 0.1
    assert not (tmp_path / "work" / "bed_warped.flac").exists()


def test_stopping_the_job_kills_the_render_at_once():
    # the render ran as a plain subprocess.run: Stop waited out an hour-long ffmpeg
    from backend.app.infrastructure.process import ProcessError
    from backend.app.services.audio import run_command

    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    with pytest.raises(ProcessError, match="canceled"):
        run_command(["sleep", "30"], cancel=stop)
    assert time.monotonic() - started < 5
