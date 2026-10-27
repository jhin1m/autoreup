import subprocess
from pathlib import Path

import pytest
from PIL import Image

from backend.app.core.config import Settings
from backend.app.pipeline.production import _hardsub_config
from backend.app.services.audio import ffmpeg_available
from backend.app.services.hardsub import build_hardsub, render_overlay_frame
from backend.app.services.overlay import CoverRegion, OverlayConfig

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg not installed")

FONT = Path(__file__).resolve().parents[1] / "backend" / "app" / "assets" / "fonts" / "Bangers-Regular.ttf"


def _settings(tmp_path) -> Settings:
    settings = Settings(root_dir=tmp_path, _env_file=None, video_width=320, video_height=240, hardsub_font=str(FONT))
    settings.ensure_directories()
    return settings


def _video(path, source="color=c=black:s=320x240:d=1.5") -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source, "-pix_fmt", "yuv420p", str(path)], check=True)


def _frame(tmp_path, *regions, captions=True, caption="", source=None) -> Image.Image:
    video, frame = tmp_path / "v.mp4", tmp_path / "frame.png"
    _video(video, *(source and [source] or []))
    overlay = OverlayConfig(caption_mode="manual", caption_y=0.5, cover=list(regions), captions=captions)
    render_overlay_frame(video, frame, 0.5, _hardsub_config(_settings(tmp_path), overlay), caption=caption)
    return Image.open(frame).convert("RGB")


def test_a_colour_is_kept_only_for_modes_that_paint_one():
    stored = OverlayConfig.from_json({"cover": [
        {"x": 0, "y": 0, "w": 0.2, "h": 0.2, "mode": "solid", "color": "#FF8800"},
        {"x": 0, "y": 0, "w": 0.2, "h": 0.2, "mode": "solid", "color": "red; drawbox"},
        {"x": 0, "y": 0, "w": 0.2, "h": 0.2, "mode": "blur", "color": "#ff8800"},
        {"x": 0, "y": 0, "w": 0.2, "h": 0.2, "mode": "nonsense"},
    ]}).to_json()["cover"]
    assert [(region["mode"], region["color"]) for region in stored] == [("solid", "#ff8800"), ("solid", ""), ("blur", ""), ("blur", "")]


def test_captions_are_on_unless_turned_off():
    assert OverlayConfig.from_json({}).captions is True
    assert OverlayConfig.from_json({"captions": None}).captions is True
    assert OverlayConfig.from_json(OverlayConfig(captions=False).to_json()).captions is False


@needs_ffmpeg
def test_a_solid_box_paints_its_colour_at_its_opacity(tmp_path):
    frame = _frame(tmp_path, CoverRegion(x=0.0, y=0.0, w=0.5, h=0.5, mode="solid", color="#ff0000"))
    red, green, blue = frame.getpixel((80, 60))
    assert red > 200 and green < 60 and blue < 60
    assert max(frame.getpixel((250, 200))) < 40
    half = _frame(tmp_path, CoverRegion(x=0.0, y=0.0, w=0.5, h=0.5, mode="solid", strength=0.5, color="#ff0000"))
    assert 90 < half.getpixel((80, 60))[0] < 170


@needs_ffmpeg
def test_frosted_glass_washes_the_box_in_its_colour(tmp_path):
    frame = _frame(tmp_path, CoverRegion(x=0.0, y=0.0, w=0.5, h=0.5, mode="frost"))
    assert min(frame.getpixel((80, 60))) > 90  # white wash over black
    assert max(frame.getpixel((250, 200))) < 40


@needs_ffmpeg
def test_pixelate_turns_the_box_into_flat_blocks(tmp_path):
    # a noisy picture: neighbouring pixels differ everywhere except inside the mosaic
    frame = _frame(tmp_path, CoverRegion(x=0.0, y=0.0, w=0.5, h=0.5, mode="pixelate"), source="color=c=gray:s=320x240:d=1.5,noise=alls=100:allf=t")
    block = [frame.getpixel((x, y)) for x in range(4, 14) for y in range(4, 14)]
    assert len(set(block)) == 1
    outside = [frame.getpixel((x, y)) for x in range(250, 260) for y in range(180, 190)]
    assert len(set(outside)) > 1


@needs_ffmpeg
def test_a_gradient_is_solid_at_the_frame_edge_and_clear_toward_the_middle(tmp_path):
    bottom = _frame(tmp_path, CoverRegion(x=0.0, y=0.5, w=1.0, h=0.5, mode="gradient", color="#ff0000"))
    assert bottom.getpixel((160, 236))[0] > 200 and bottom.getpixel((160, 124))[0] < 40
    top = _frame(tmp_path, CoverRegion(x=0.0, y=0.0, w=1.0, h=0.5, mode="gradient", color="#ff0000"))
    assert top.getpixel((160, 2))[0] > 200 and top.getpixel((160, 116))[0] < 40


@needs_ffmpeg
def test_turning_captions_off_keeps_covers_and_drops_the_text(tmp_path):
    box = CoverRegion(x=0.0, y=0.0, w=0.3, h=0.3, mode="solid", color="#ff0000")
    shown = _frame(tmp_path, box, caption="XIN CHAO XIN CHAO")
    hidden = _frame(tmp_path, box, captions=False, caption="XIN CHAO XIN CHAO")
    band = [(x, y) for x in range(40, 280, 2) for y in range(100, 140, 2)]
    assert max(max(shown.getpixel(xy)) for xy in band) > 200
    assert max(max(hidden.getpixel(xy)) for xy in band) < 40
    assert hidden.getpixel((40, 30))[0] > 200


@needs_ffmpeg
def test_a_render_with_captions_off_burns_no_text(tmp_path):
    video, out = tmp_path / "v.mp4", tmp_path / "out.mp4"
    _video(video)
    overlay = OverlayConfig(caption_mode="manual", caption_y=0.5, captions=False)
    summary = build_hardsub(video, [{"start": 0.0, "end": 1.5, "text": "XIN CHAO"}], None, out, tmp_path / "work",
                            _hardsub_config(_settings(tmp_path), overlay))
    assert summary["cues"] == 0
    frame = tmp_path / "out.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-i", str(out), "-frames:v", "1", str(frame)], check=True)
    image = Image.open(frame).convert("RGB")
    assert max(max(image.getpixel((x, y))) for x in range(40, 280, 2) for y in range(100, 140, 2)) < 40
