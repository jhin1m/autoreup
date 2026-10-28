import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

from backend.app.core.config import Settings
from backend.app.dubbing.time_warp import TimeWarp, WarpSegment
from backend.app.pipeline.production import _hardsub_config
from backend.app.services.audio import ffmpeg_available
from backend.app.services.hardsub import _build_cover_chain, build_hardsub, render_overlay_frame
from backend.app.services.overlay import CoverRegion, OverlayConfig
from backend.app.services.smart_cover import SmartCoverConfig, _extent, _Run, _smooth, detect_text_spans, mask_png

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")

FONT = Path(__file__).resolve().parents[1] / "backend" / "app" / "assets" / "fonts" / "Bangers-Regular.ttf"
BOX = (0, 168, 320, 60)  # the smart region below, in output pixels
SMART = CoverRegion(x=0.0, y=0.7, w=1.0, h=0.25, mode="smart")


def _run(start, end, cols, width=100):
    hits = np.zeros(width, dtype=np.int32)
    for lo, hi, count in cols:
        hits[lo:hi] = count
    return _Run(start, end, hits)


def test_flicker_gaps_merge_short_runs_drop_and_rolls_pad():
    config = SmartCoverConfig()
    runs = [
        _run(10, 20, [(40, 60, 10)]),
        _run(22, 30, [(40, 60, 8)]),   # 0.2 s gap: the same caption flickering
        _run(50, 51, [(40, 60, 1)]),   # one frame: noise
    ]
    spans = _smooth(runs, 100, 100, 10, config)
    assert len(spans) == 1
    assert spans[0].start == pytest.approx(1.0 - config.pre_roll)
    assert spans[0].end == pytest.approx(3.0 + config.post_roll)
    assert (spans[0].x0, spans[0].x1) == (35, 65)  # widened by half a box height each side


def test_rolls_never_leave_the_video():
    spans = _smooth([_run(0, 10, [(0, 100, 10)])], 10, 100, 10, SmartCoverConfig())
    assert (spans[0].start, spans[0].end) == (0.0, 1.0)


def test_a_passing_highlight_does_not_widen_the_caption_and_no_steady_column_covers_all():
    config = SmartCoverConfig(x_pad_frac=0)
    assert _extent(_run(0, 10, [(40, 60, 10), (90, 95, 1)]).cols, 10, 100, 10, config) == (40, 60)
    assert _extent(_run(0, 10, [(90, 95, 1)]).cols, 10, 100, 10, config) == (0, 100)


def test_a_short_clear_between_two_captions_is_bridged_so_the_blur_does_not_flicker():
    config = SmartCoverConfig()
    spans = _smooth([_run(10, 20, [(40, 60, 10)]), _run(26, 36, [(40, 60, 10)])], 100, 100, 10, config)
    assert len(spans) == 1  # 0.6 s apart, 0.25 s once the rolls are added
    spans = _smooth([_run(10, 20, [(40, 60, 10)]), _run(40, 50, [(40, 60, 10)])], 100, 100, 10, config)
    assert len(spans) == 2


def test_a_corner_watermark_does_not_stretch_the_caption_cover():
    config = SmartCoverConfig(x_pad_frac=0)
    cols = _run(0, 10, [(2, 20, 10), (40, 60, 10)]).cols  # watermark at the edge, caption in the middle
    assert _extent(cols, 10, 100, 10, config) == (40, 60)
    off_centre = _run(0, 10, [(2, 20, 10)]).cols  # only off-centre text: cover it anyway
    assert _extent(off_centre, 10, 100, 10, config) == (2, 20)


def test_the_mask_is_white_over_the_caption_and_fades_at_the_edges():
    mask = np.asarray(mask_png((20, 80), 100, 60))
    assert mask[30, 50] == 255
    assert mask[30, 2] == 0 and mask[30, 98] == 0
    assert 0 < mask[30, 20] < 255  # feathered edge
    assert not np.asarray(mask_png(None, 100, 60)).any()


def test_plain_modes_keep_their_chain_and_a_smart_region_alpha_merges_through_its_mask():
    config = _hardsub_config(Settings(_env_file=None), OverlayConfig())
    regions = [(0, 100, 320, 60, "blur", 1.0, ""), (0, 100, 320, 60, "smart", 1.0, "")]
    plain = _build_cover_chain(regions, config)
    assert "alphamerge" not in plain  # a smart region without a mask is a plain blur
    assert plain.split(";[v1]")[0] == _build_cover_chain(regions[:1], config).split(";[v1]")[0]
    smart = _build_cover_chain(regions, config, {1: "[3:v]"})
    assert "[3:v]format=gray" in smart and "alphamerge" in smart and "noise=" in smart


def _settings(tmp_path) -> Settings:
    settings = Settings(root_dir=tmp_path, _env_file=None, video_width=320, video_height=240, hardsub_font=str(FONT))
    settings.ensure_directories()
    return settings


def _video(tmp_path) -> Path:
    """3 s of a textured picture with no caption-like pixels (contrast squeezed into
    60..162), and a white caption with a black outline from 1 s to 2 s."""
    text = Image.new("RGBA", (200, 40), (0, 0, 0, 0))
    ImageDraw.Draw(text).text((10, 4), "HELLO WORLD", font=ImageFont.truetype(str(FONT), 28), fill="white", stroke_width=3, stroke_fill="black")
    text.save(tmp_path / "text.png")
    video = tmp_path / "v.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=3:r=25", "-i", str(tmp_path / "text.png"),
        "-filter_complex", "[0:v]lutyuv=y=val*0.4+60[bg];[bg][1:v]overlay=60:180:enable='between(t,1,2)'",
        "-pix_fmt", "yuv420p", str(video),
    ], check=True)
    return video


def _box_diff(a: Path, b: Path, t: float, tmp_path: Path) -> float:
    """Mean gray difference inside BOX between ``a`` at ``t`` and the image/video ``b``."""
    def grab(src: Path, name: str) -> Image.Image:
        if src.suffix == ".png":
            return Image.open(src)
        out = tmp_path / f"{name}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t}", "-i", str(src), "-frames:v", "1", str(out)], check=True)
        return Image.open(out)

    x, y, w, h = BOX
    left, right = (grab(src, name).convert("L").crop((x, y, x + w, y + h)) for src, name in ((a, "a"), (b, "b")))
    return ImageStat.Stat(ImageChops.difference(left, right)).mean[0]


def _config(tmp_path, *regions):
    return _hardsub_config(_settings(tmp_path), OverlayConfig(caption_mode="manual", caption_y=0.5, cover=list(regions), captions=False))


@needs_ffmpeg
def test_detection_finds_the_caption_span_and_its_columns(tmp_path):
    video = _video(tmp_path)
    spans = detect_text_spans(video, BOX, [_config(tmp_path, SMART).scale_filter])
    assert len(spans) == 1
    assert 0.7 <= spans[0].start <= 1.0 and 2.0 <= spans[0].end <= 2.4
    assert spans[0].x0 <= 70 and spans[0].x1 >= 190


@needs_ffmpeg
def test_the_render_blurs_only_while_the_caption_shows(tmp_path):
    video = _video(tmp_path)
    out = tmp_path / "out.mp4"
    summary = build_hardsub(video, [], None, out, tmp_path / "work", _config(tmp_path, SMART))
    assert summary["smart"] == [{"region": 0, "spans": 1, "covered_s": pytest.approx(1.4, abs=0.4)}]
    assert _box_diff(video, out, 0.5, tmp_path) < 4    # before the caption: the picture as it was
    assert _box_diff(video, out, 1.5, tmp_path) > 15   # the caption: blurred
    assert _box_diff(video, out, 2.7, tmp_path) < 4    # after it: clean again


@needs_ffmpeg
def test_the_blur_follows_the_stretched_picture(tmp_path):
    video = _video(tmp_path)
    out = tmp_path / "out.mp4"
    warp = TimeWarp([WarpSegment(start=0.0, length=0.5, factor=2.0)])  # the first half second plays twice as long
    build_hardsub(video, [], None, out, tmp_path / "work", _config(tmp_path, SMART), warp=warp)
    # output 0.9 s = source 0.45 s (no caption yet); output 2.0 s = source 1.5 s (caption)
    source_early, source_caption = tmp_path / "s045.png", tmp_path / "s15.png"
    for t, path in ((0.45, source_early), (1.5, source_caption)):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t}", "-i", str(video), "-frames:v", "1", str(path)], check=True)
    assert _box_diff(out, source_early, 0.9, tmp_path) < 4
    assert _box_diff(out, source_caption, 2.0, tmp_path) > 15


@needs_ffmpeg
def test_a_smart_region_that_finds_no_text_blurs_the_whole_video(tmp_path):
    video = _video(tmp_path)
    out = tmp_path / "out.mp4"
    top = CoverRegion(x=0.0, y=0.0, w=1.0, h=0.25, mode="smart")
    summary = build_hardsub(video, [], None, out, tmp_path / "work", _config(tmp_path, top))
    assert summary["smart"][0]["fallback"] == "no text found"
    frame = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(out), "-frames:v", "1", str(frame)], check=True)
    source = tmp_path / "s.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(video), "-frames:v", "1", str(source)], check=True)
    top_box = (0, 0, 320, 60)
    diff = ImageChops.difference(Image.open(frame).convert("L").crop(top_box), Image.open(source).convert("L").crop(top_box))
    assert ImageStat.Stat(diff).mean[0] > 4


@needs_ffmpeg
def test_the_preview_shows_a_smart_region_as_the_render_does_at_that_moment(tmp_path):
    video = _video(tmp_path)
    config = _config(tmp_path, SMART)
    for t, blurred in ((0.5, False), (1.5, True)):
        frame = tmp_path / f"p{t}.png"
        render_overlay_frame(video, frame, t, config)
        diff = _box_diff(video, frame, t, tmp_path)
        assert (diff > 15) if blurred else (diff < 4)


def _caption_at(tmp_path, start: float, fps: int = 30) -> Path:
    """Like _video, at ``fps``, with the caption from ``start`` to 2 s."""
    text = Image.new("RGBA", (200, 40), (0, 0, 0, 0))
    ImageDraw.Draw(text).text((10, 4), "HELLO WORLD", font=ImageFont.truetype(str(FONT), 28), fill="white", stroke_width=3, stroke_fill="black")
    text.save(tmp_path / "text.png")
    video = tmp_path / "v30.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=320x240:d=3:r={fps}", "-i", str(tmp_path / "text.png"),
        "-filter_complex", f"[0:v]lutyuv=y=val*0.4+60[bg];[bg][1:v]overlay=60:180:enable='gte(t,{start})*lte(t,2)'",
        "-pix_fmt", "yuv420p", str(video),
    ], check=True)
    return video


def _caption_area(video: Path, frame: int, tmp_path: Path) -> np.ndarray:
    """The caption's own pixels (where _caption_at draws it) on frame ``frame``."""
    out = tmp_path / "n.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", f"select=eq(n\\,{frame})", "-frames:v", "1", str(out)], check=True)
    return np.asarray(Image.open(out).convert("L").crop((60, 180, 260, 220)), dtype=np.float32)


@needs_ffmpeg
def test_the_first_frame_of_a_caption_is_already_covered(tmp_path):
    # first shown on frame 32 (1.0667 s at 30 fps): one source frame after a 10 fps
    # detection sample, between two ms-rounded mask timestamps — the cover's worst lag.
    # Measured against a plain blur (the fully covered look): 0.15 s of pre-roll left a
    # quarter of the caption showing (mean diff 16 vs ~5 of grain once fully covered).
    video = _caption_at(tmp_path, 1.066)
    covered = {}
    for mode in ("smart", "blur"):
        out = tmp_path / f"{mode}.mp4"
        build_hardsub(video, [], None, out, tmp_path / f"work_{mode}", _config(tmp_path, CoverRegion(x=0.0, y=0.7, w=1.0, h=0.25, mode=mode)))
        covered[mode] = _caption_area(out, 32, tmp_path)
    assert np.abs(_caption_area(video, 32, tmp_path) - covered["blur"]).mean() > 40  # the caption is there
    assert np.abs(covered["smart"] - covered["blur"]).mean() < 8


@needs_ffmpeg
def test_an_odd_sized_box_at_the_frame_edge_still_gets_its_smart_cover(tmp_path):
    video = _video(tmp_path)
    out = tmp_path / "out.mp4"
    odd = CoverRegion(x=0.0031, y=0.7, w=0.9969, h=0.2542, mode="smart")  # 319x61 px, touching the right edge
    summary = build_hardsub(video, [], None, out, tmp_path / "work", _config(tmp_path, odd))
    assert summary["smart"][0]["spans"] == 1 and "fallback" not in summary["smart"][0]
    assert not (tmp_path / "work" / "smart").exists()  # masks are cleaned up


@needs_ffmpeg
def test_a_failed_mask_falls_back_to_a_blur_but_a_stop_still_stops(tmp_path, monkeypatch):
    import threading

    from backend.app.infrastructure.process import ProcessError
    from backend.app.services import smart_cover

    def broken(*_args, **_kwargs):
        raise ProcessError("Command failed with exit code 1: ffmpeg")

    monkeypatch.setattr(smart_cover, "build_mask_video", broken)
    video = _video(tmp_path)
    cancel = threading.Event()
    summary = build_hardsub(video, [], None, tmp_path / "out.mp4", tmp_path / "work", _config(tmp_path, SMART), cancel=cancel)
    assert summary["smart"][0]["fallback"].startswith("smart cover failed")
    cancel.set()
    with pytest.raises(ProcessError):
        build_hardsub(video, [], None, tmp_path / "out2.mp4", tmp_path / "work2", _config(tmp_path, SMART), cancel=cancel)
