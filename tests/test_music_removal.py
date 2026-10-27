import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from backend.app.core.config import Settings
from backend.app.pipeline.contracts import StageCanceled
from backend.app.services import music_removal
from backend.app.services.audio import ffmpeg_available, probe_duration
from backend.app.services.hardsub import HardsubConfig
from backend.app.services.overlay import OverlayConfig
from tests.test_hardsub_cover import _captured_hardsub

torch = pytest.importorskip("torch")
needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")


class _HalfIsMusic(torch.nn.Module):
    """Stand-in separator: calls half of every chunk "music" (so the bed is half the mid)."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def forward(self, audio):
        self.calls += 1
        return audio * 0.5


def _fake_model(monkeypatch, model=None):
    model = model or _HalfIsMusic()
    monkeypatch.setattr(music_removal, "_load_model", lambda settings, report, stopped: (model, torch.device("cpu")))
    return model


def _source(path: Path, seconds: float) -> None:
    # left and right differ: the bed is built from their mid (L+R)/2
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f=330:sample_rate=48000:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=f=550:sample_rate=48000:duration={seconds}",
         "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]", "-map", "[a]", str(path)],
        check=True,
    )


def _decode_left(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-af", "pan=mono|c0=c0", "-ar", "48000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


@needs_ffmpeg
def test_the_bed_is_the_mid_minus_the_music_across_chunk_joins(tmp_path, monkeypatch):
    settings = Settings(root_dir=tmp_path, _env_file=None)
    source = tmp_path / "source.wav"
    _source(source, 130.0)  # two batches of 8 chunks: joins inside and between batches
    _fake_model(monkeypatch)

    bed = music_removal.ensure_bed(settings, "job1", source)

    assert bed == settings.data_dir / "audio" / "job1.nomusic.m4a"
    assert probe_duration(bed) == pytest.approx(130.0, abs=0.05)
    left, right = _decode_left(source), _decode_left(bed)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-af", "pan=mono|c0=0.5*c0+0.5*c1", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    expected = np.frombuffer(raw, np.float32) * 0.5  # crossfade weights sum to 1 everywhere
    n = min(len(expected), len(right))
    assert n == pytest.approx(len(left), abs=2048)
    error = np.sqrt(np.mean((right[:n] - expected[:n]) ** 2)) / np.sqrt(np.mean(expected[:n] ** 2))
    assert error < 0.05  # AAC, not the overlap-add, is all that is left


@needs_ffmpeg
def test_the_bed_is_computed_once_per_job(tmp_path, monkeypatch):
    settings = Settings(root_dir=tmp_path, _env_file=None)
    source = tmp_path / "source.wav"
    _source(source, 10.0)
    model = _fake_model(monkeypatch)

    first = music_removal.ensure_bed(settings, "job1", source)
    calls = model.calls
    assert music_removal.ensure_bed(settings, "job1", source) == first
    assert model.calls == calls  # a re-render reuses it


@needs_ffmpeg
def test_the_render_waits_for_a_bed_already_being_made(tmp_path, monkeypatch):
    settings = Settings(root_dir=tmp_path, _env_file=None)
    source = tmp_path / "source.wav"
    _source(source, 10.0)
    release = threading.Event()

    class Slow(_HalfIsMusic):
        def forward(self, audio):
            release.wait(5)
            return super().forward(audio)

    model = _fake_model(monkeypatch, Slow())
    music_removal.prefetch(settings, "job1", source)
    waiter = threading.Thread(target=music_removal.ensure_bed, args=(settings, "job1", source))
    waiter.start()
    release.set()
    waiter.join(10)

    assert not waiter.is_alive()
    assert music_removal.bed_path(settings, "job1").exists()
    assert model.calls == 1  # the render waited for the prefetch instead of starting its own


@needs_ffmpeg
def test_a_stopped_bed_leaves_no_file(tmp_path, monkeypatch):
    settings = Settings(root_dir=tmp_path, _env_file=None)
    source = tmp_path / "source.wav"
    _source(source, 130.0)
    cancel = threading.Event()

    class StopAfterFirstBatch(_HalfIsMusic):
        def forward(self, audio):
            cancel.set()
            return super().forward(audio)

    _fake_model(monkeypatch, StopAfterFirstBatch())
    with pytest.raises(StageCanceled):
        music_removal.ensure_bed(settings, "job1", source, cancel=cancel)
    assert list((settings.data_dir / "audio").iterdir()) == []


def test_the_episode_choice_overrides_the_app_setting(tmp_path):
    on = Settings(root_dir=tmp_path, _env_file=None, dub_remove_music=True)
    off = Settings(root_dir=tmp_path, _env_file=None)
    assert music_removal.wanted(on, None) and not music_removal.wanted(off, None)
    assert music_removal.wanted(off, OverlayConfig(remove_music=True))
    assert not music_removal.wanted(on, OverlayConfig(remove_music=False))
    assert OverlayConfig.from_json(OverlayConfig(remove_music=True).to_json()).remove_music is True
    assert OverlayConfig.from_json({}).remove_music is None  # unset follows the app setting


def test_the_render_mixes_the_music_free_bed_instead_of_the_source_audio(monkeypatch, tmp_path):
    config = HardsubConfig(sharpen=0.0, bed_audio=str(tmp_path / "job.nomusic.m4a"))
    inputs, graph = _captured_hardsub(monkeypatch, tmp_path, None, config=config)
    assert [Path(p).name for p in inputs] == ["v.mp4", "vi_overlay.mov", "voice.m4a", "job.nomusic.m4a"]
    assert "[3:a:0]aresample" in graph and "[0:a:0]" not in graph


def test_a_stretched_render_stretches_the_music_free_bed(monkeypatch, tmp_path):
    import backend.app.services.audio as audio
    from backend.app.dubbing.time_warp import TimeWarp, WarpSegment

    import backend.app.services.hardsub as hardsub

    sources = []
    original = hardsub.build_hardsub

    def build(*args, **kwargs):  # the helper stubs warp_audio; record what it is handed
        stub = audio.warp_audio
        monkeypatch.setattr(audio, "warp_audio", lambda source, warp, out, work: sources.append(Path(source)) or stub(source, warp, out, work))
        return original(*args, **kwargs)

    monkeypatch.setattr(hardsub, "build_hardsub", build)
    config = HardsubConfig(sharpen=0.0, bed_audio=str(tmp_path / "job.nomusic.m4a"))
    _captured_hardsub(monkeypatch, tmp_path, TimeWarp([WarpSegment(0.0, 1.0, 1.1)]), config=config)
    assert [source.name for source in sources] == ["job.nomusic.m4a"]
