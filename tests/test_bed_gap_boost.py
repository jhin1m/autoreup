import subprocess

import numpy as np
import pytest

import backend.app.dubbing.onset as onset
from backend.app.core.config import Settings
from backend.app.pipeline.production import _hardsub_config
from backend.app.services.audio import (
    GAIN_RATE,
    dub_mix_filter,
    ffmpeg_available,
    gap_gain_envelope,
    render_gap_gain,
    speech_gaps,
    voice_regions,
)
from backend.app.services.overlay import OverlayConfig

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")


def _render(path, source: str, duration: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source, "-t", str(duration), "-ac", "2", "-ar", "44100", str(path)],
        check=True,
    )


def _decode(path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "44100", "pipe:1"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<f4")


def _peak(samples: np.ndarray, start: float, end: float) -> float:
    return float(np.abs(samples[int(start * 44100):int(end * 44100)]).max())


def test_only_long_quiet_stretches_count_as_gaps():
    regions = [(1.0, 2.0), (2.8, 4.0), (10.0, 11.0)]
    # 2.0-2.8 is padded shut; 4.0-10.0 is a gap; so are the head and the tail
    assert speech_gaps(regions, 14.0, pad=0.3, min_gap=1.5) == [(4.3, 9.7), (11.3, 14.0)]
    assert speech_gaps(regions, 12.0, pad=0.3, min_gap=1.5) == [(4.3, 9.7)]  # 0.7s tail: too short
    assert speech_gaps([], 5.0) == [(0.0, 5.0)]
    assert speech_gaps([(0.0, 5.0)], 5.0) == []


def test_the_envelope_holds_the_bed_under_speech_and_ramps_inside_the_gap():
    envelope = gap_gain_envelope([(0.0, 2.0), (5.0, 8.0)], 10.0, bed=0.5, gap=1.0, ramp=0.4)
    at = lambda t: float(envelope[int(round(t * GAIN_RATE))])
    assert at(0.0) == pytest.approx(1.0)  # no fade-in at the very start of the video
    assert at(1.0) == pytest.approx(1.0)
    assert at(1.8) == pytest.approx(0.75, abs=0.01)  # halfway down the ramp before speech
    assert at(3.0) == pytest.approx(0.5)  # speech: the bed level
    assert at(5.2) == pytest.approx(0.75, abs=0.01)
    assert at(6.5) == pytest.approx(1.0)
    assert at(9.0) == pytest.approx(0.5)


@needs_ffmpeg
def test_the_dub_track_speaks_where_it_is_audible(tmp_path):
    voice = tmp_path / "voice.m4a"
    _render(voice, "aevalsrc='0.3*sin(2*PI*220*t)*between(t,2,3)':s=44100", 5.0)
    [(start, end)] = voice_regions(voice)
    assert start == pytest.approx(2.0, abs=0.05) and end == pytest.approx(3.0, abs=0.05)


@needs_ffmpeg
def test_the_render_brings_the_bed_up_where_nobody_speaks(tmp_path, monkeypatch):
    video = tmp_path / "source.wav"  # lossless: AAC overshoot would blur the levels
    voice = tmp_path / "voice.wav"
    gain = tmp_path / "gain.wav"
    out = tmp_path / "mix.wav"
    _render(video, "sine=f=440:sample_rate=44100", 8.0)  # music only: peak 1/8
    _render(voice, "aevalsrc='0.3*sin(2*PI*220*t)*between(t,0.5,2)':s=44100", 8.0)
    probs = np.zeros(int(8.0 / 0.032), dtype=np.float32)
    monkeypatch.setattr(onset, "_speech_probs", lambda path: (probs, 0.032))

    stats = render_gap_gain(video, voice, gain, bed=0.4, gap=1.0)
    assert stats == {"gaps": 1, "seconds": pytest.approx(5.7, abs=0.1)}
    graph = dub_mix_filter("[0:a:0]", "[1:a:0]", "[aout]", 0.4, duck=False, carve_db=0, bed_gain="[2:a:0]")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(voice), "-i", str(gain),
         "-filter_complex", graph, "-map", "[aout]", str(out)],
        check=True,
    )
    mix = _decode(out)
    assert len(mix) == pytest.approx(8.0 * 44100, abs=4096)  # the bed sets the length, not the envelope
    # the head (0-0.2s, before the voice) is too short for a gap: it stays at the bed
    head, gap = _peak(mix, 0.05, 0.2), _peak(mix, 4.0, 7.5)
    assert gap / head == pytest.approx(1.0 / 0.4, rel=0.05)


@needs_ffmpeg
def test_no_source_vad_means_no_boost(tmp_path, monkeypatch):
    voice = tmp_path / "voice.m4a"
    _render(voice, "anullsrc=r=44100", 3.0)
    monkeypatch.setattr(onset, "_speech_probs", lambda path: None)
    assert render_gap_gain(voice, voice, tmp_path / "gain.wav", bed=0.4, gap=1.0) is None


def test_the_gap_level_comes_from_the_editor_or_the_app_setting(tmp_path):
    settings = Settings(root_dir=tmp_path, _env_file=None, dub_gap_volume=0.9)
    assert _hardsub_config(settings, OverlayConfig.from_json({})).gap_volume == 0.9
    assert _hardsub_config(settings, OverlayConfig.from_json({"gap_volume": 0.7})).gap_volume == 0.7
    # turned off in the editor: stays off instead of falling back to the app setting
    assert _hardsub_config(settings, OverlayConfig.from_json({"gap_volume": 0})).gap_volume == 0.0
    assert OverlayConfig.from_json(OverlayConfig(gap_volume=0.6).to_json()).gap_volume == 0.6


@needs_ffmpeg
def test_the_warped_bed_has_the_exact_output_length(tmp_path):
    from backend.app.dubbing.time_warp import TimeWarp, WarpSegment
    from backend.app.services.audio import warp_audio

    source = tmp_path / "source.wav"
    _render(source, "sine=f=440:sample_rate=48000", 10.0)  # not the mix rate: pieces resample first
    warp = TimeWarp([WarpSegment(1.0, 1.5, 1.111), WarpSegment(4.0, 0.7, 1.05), WarpSegment(8.2, 2.5, 1.1)])
    out = warp_audio(source, warp, tmp_path / "bed.flac", tmp_path / "pieces")
    assert len(_decode(out)) == pytest.approx(warp.to_output(10.0) * 44100, abs=44)  # ±1 ms
    assert not list((tmp_path / "pieces").glob("*.wav"))  # pieces cleaned up


@needs_ffmpeg
def test_the_gap_envelope_follows_the_stretched_picture(tmp_path, monkeypatch):
    from backend.app.dubbing.time_warp import TimeWarp, WarpSegment

    video = tmp_path / "source.wav"
    voice = tmp_path / "voice.wav"
    _render(video, "sine=f=440:sample_rate=44100", 10.0)
    _render(voice, "anullsrc=r=44100", 11.0)
    probs = np.zeros(int(10.0 / 0.032), dtype=np.float32)
    probs[int(6.0 / 0.032):int(7.0 / 0.032)] = 1.0  # source speech 6-7s
    monkeypatch.setattr(onset, "_speech_probs", lambda path: (probs, 0.032))

    plain = render_gap_gain(video, voice, tmp_path / "a.wav", bed=0.4, gap=1.0)
    warp = TimeWarp([WarpSegment(1.0, 2.0, 1.5)])  # +1s before the speech
    warped = render_gap_gain(video, voice, tmp_path / "b.wav", bed=0.4, gap=1.0, warp=warp)
    assert warped["gaps"] == plain["gaps"] == 2
    assert warped["seconds"] - plain["seconds"] == pytest.approx(1.0, abs=0.15)


@needs_ffmpeg
def test_the_warped_bed_has_no_silence_at_the_joins(tmp_path):
    from backend.app.dubbing.time_warp import TimeWarp, WarpSegment
    from backend.app.services.audio import warp_audio

    source = tmp_path / "source.m4a"  # AAC, like real sources: an input -t cut its pieces short
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:duration=12",
                    "-c:a", "aac", "-b:a", "192k", str(source)], check=True)
    warp = TimeWarp([WarpSegment(1.3, 1.7, 1.111), WarpSegment(5.05, 2.2, 1.08), WarpSegment(9.4, 1.1, 1.1)])
    samples = _decode(warp_audio(source, warp, tmp_path / "bed.flac", tmp_path / "pieces"))
    window = int(0.002 * 44100)
    loud = np.abs(samples[: len(samples) // window * window]).reshape(-1, window).max(axis=1)
    tone_end = int(warp.to_output(11.9) / 0.002)  # the AAC container runs a little past the tone
    assert loud[5:tone_end].min() > 0.05  # every 2 ms of the bed carries the tone (peak 1/8)
    assert not (tmp_path / "pieces").exists()
