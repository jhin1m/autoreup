"""Hardsub: cover the original (burned-in) subtitle and burn the Vietnamese one.

No libass in the ffmpeg build, so text is rendered with PIL into transparent PNGs
and composited as a single alpha overlay video (concat of held frames — efficient
for hundreds of cues). The original subtitle's band is found by a lightweight cv2
detector (bright text-with-dark-outline rows that recur across many frames — subs
are consistent, scenes vary) and covered with a strong blur + slight darken.

Pipeline: original video -> blur/darken the sub band (always on) -> overlay the VI
sub video -> mux the dubbed audio. Falls back gracefully if the band isn't found
(then it just burns the VI sub near the bottom without covering).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from backend.app.services.audio import run_command


@lru_cache(maxsize=1)
def preferred_video_codec_args() -> tuple[str, ...]:
    """H.264 encoder args — chosen by MEASUREMENT, not by platform folklore.

    Benchmarked on this machine (60s of 1080p30, 2026-09): libx264 -veryfast -crf 20
    takes 19.6s, h264_videotoolbox -b:v 8M takes 29.9s — the hardware encoder is
    SLOWER here, so the software encoder stays the default. (VideoToolbox remains one
    edit away if a future machine/profile flips that; measure before switching.)"""
    return ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p")


@dataclass(slots=True)
class HardsubConfig:
    font_path: str = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"  # bold = clearer on video
    font_size: int = 50
    margin_v: int = 88        # caption baseline distance from the bottom when no band
    sample_frames: int = 40
    detect_search_top: float = 0.62  # search the bottom (1-this) of the frame
    band_pad: int = 26        # extra px around the detected band so the original sub's outline is fully covered
    blur: int = 20            # boxblur radius (ffmpeg caps at 23); applied twice for strong coverage
    darken: float = 0.12
    bed_volume: float = 0.55
    duck: bool = True
    carve_db: float = -3.0
    # Encoder + canonical geometry (filled from Settings). See preferred_video_codec_args:
    # source-resolution behaviour so hardsub works standalone.
    video_codec_args: tuple[str, ...] = field(default_factory=preferred_video_codec_args)
    scale_filter: str = ""  # empty = keep source size; else scale/pad/fps to a canonical output
    # The overlay is rendered in THIS space (settings.video_width/height); 0 = source.
    # Measured bug this fixes: a 3840x2160 source built its overlay at source size with
    # font 58, then the canonical scale halved it -> captions burned at ~29px, while a
    # 1080p source got the full 58px. Caption size must not depend on the source size.
    out_width: int = 0
    out_height: int = 0
    # Contrast-adaptive sharpening on the picture (NOT the caption overlay, which is
    # already crisp). Douyin sources sit around 1Mbps for 1080p — visibly soft; a
    # light CAS restores perceived detail without unsharp's halos. 0 disables.
    sharpen: float = 0.35


def detect_sub_band(video: Path, config: HardsubConfig) -> tuple[int, int, int, int] | None:
    """Return (band_y, band_h, width, height) of the original subtitle, or None.

    A row is 'subtitle' in a frame if it has a wide enough run of bright pixels that
    sit next to a dark outline, in the centre 64%. The band is the rows where that
    recurs across many sample frames."""
    import cv2  # local import: cv2 is only needed for hardsub
    import numpy as np

    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width == 0 or height == 0:
        cap.release()
        return None

    y0 = int(height * config.detect_search_top)
    cx0, cx1 = int(width * 0.18), int(width * 0.82)
    min_run = int((cx1 - cx0) * 0.12)
    freq = np.zeros(height, dtype=np.float64)
    frames = max(8, config.sample_frames)
    for index in range(frames):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (index + 0.5) / frames))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        bright = (gray >= 195).astype(np.uint8)
        dark = cv2.dilate((gray <= 85).astype(np.uint8), np.ones((7, 7), np.uint8))
        text = bright & dark
        rowsum = text[:, cx0:cx1].sum(axis=1)
        has = (rowsum >= min_run).astype(np.float64)
        has[:y0] = 0
        freq += has
    cap.release()

    peak = freq.max()
    if peak < frames * 0.25:
        return None
    band = np.where(freq >= peak * 0.45)[0]
    if len(band) == 0:
        return None
    band_y = max(0, int(band.min()) - config.band_pad)
    band_bottom = min(height, int(band.max()) + config.band_pad)
    return band_y, band_bottom - band_y, width, height


@dataclass(slots=True)
class OverlayGeometry:
    out_width: int
    out_height: int
    scale: float      # source pixels -> output pixels
    pad_x: int
    pad_y: int
    caption_cy: int   # in OUTPUT pixels


def overlay_geometry(src_width: int, src_height: int, band: tuple[int, int, int, int] | None, config: HardsubConfig) -> OverlayGeometry:
    """Where the caption lives in the OUTPUT frame (matches force_original_aspect_ratio)."""
    out_width = config.out_width or src_width
    out_height = config.out_height or src_height
    scale = min(out_width / max(1, src_width), out_height / max(1, src_height))
    pad_x = (out_width - round(src_width * scale)) // 2
    pad_y = (out_height - round(src_height * scale)) // 2
    if band is not None:
        _band_y, band_h = band[0], band[1]
        caption_cy = pad_y + round((band[0] + band_h / 2) * scale)
    else:
        caption_cy = out_height - config.margin_v
    return OverlayGeometry(out_width, out_height, round(scale, 6), pad_x, pad_y, caption_cy)


def _build_overlay_video(cues: list[dict], sub_dir: Path, width: int, height: int, caption_cy: int, output: Path, config: HardsubConfig) -> None:
    """Render each cue to a transparent full-frame PNG and concat (held for each cue's
    duration, with transparent gaps) into one alpha overlay video."""
    from PIL import Image, ImageDraw, ImageFont

    sub_dir.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(config.font_path, config.font_size)
    blank = sub_dir / "blank.png"
    Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(blank)

    # Anchor every caption to a FIXED baseline from the font's own metrics — NOT each
    # line's bounding box. The bbox height varies with the glyphs present (a comma/period
    # adds a descender, Vietnamese tone marks add height), so centering by bbox made the
    # text jump up/down between cues. A constant baseline keeps it rock-steady; only
    # ascenders/descenders extend from it, which is how subtitles are supposed to sit.
    ascent, descent = font.getmetrics()
    baseline_y = caption_cy + (ascent - descent) // 2  # center the fixed line-box on the band

    def render(text: str, path: Path) -> None:
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.text((width // 2, baseline_y), text, font=font, anchor="ms",
                  fill=(255, 255, 255, 255),
                  stroke_width=max(2, round(config.font_size * 0.05)),
                  stroke_fill=(0, 0, 0, 255))
        img.save(path)

    ordered = sorted([c for c in cues if (c.get("text") or "").strip()], key=lambda c: float(c["start"]))
    entries: list[tuple[Path, float]] = []
    cursor = 0.0
    for index, cue in enumerate(ordered):
        start, end = float(cue["start"]), float(cue["end"])
        if start > cursor:
            entries.append((blank, start - cursor))
        png = sub_dir / f"c{index:05d}.png"
        render(cue["text"], png)
        entries.append((png, max(0.05, end - start)))
        cursor = end

    concat = sub_dir / "concat.txt"
    with concat.open("w", encoding="utf-8") as handle:
        for path, duration in entries:
            handle.write(f"file '{path.resolve().as_posix()}'\nduration {duration:.3f}\n")
        handle.write(f"file '{entries[-1][0].resolve().as_posix()}'\n")

    run_command([
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
        "-vsync", "vfr", "-c:v", "qtrle", "-pix_fmt", "argb", str(output),
    ])


def build_hardsub(video: Path, cues: list[dict], voice_track: Path | None, output: Path, work_dir: Path, config: HardsubConfig | None = None) -> dict:
    """Produce a hardsubbed MP4: cover the original sub band, burn the VI sub, mux the
    dubbed audio (original as a quiet bed under the VI voice). Returns a small summary."""
    config = config or HardsubConfig()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    import cv2

    cap = cv2.VideoCapture(str(video))
    src_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
    src_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    cap.release()
    geometry = overlay_geometry(src_width, src_height, None, config)

    # The band is detected in SOURCE pixels; pad/blur are tuned for the OUTPUT, so they
    # are scaled into source space before use (a 4K source needs 2x the pixel pad to
    # cover the same outline once the frame is scaled down).
    scale = geometry.scale or 1.0
    band = detect_sub_band(video, replace(config, band_pad=max(1, round(config.band_pad / scale))))
    if band is not None:
        band_y, band_h = band[0], band[1]
    else:
        band_y = band_h = 0
    geometry = overlay_geometry(src_width, src_height, band, config)

    overlay_mov = work_dir / "vi_overlay.mov"
    _build_overlay_video(cues, work_dir / "cues", geometry.out_width, geometry.out_height, geometry.caption_cy, overlay_mov, config)

    has_voice = voice_track is not None and Path(voice_track).exists()
    inputs = ["-i", str(video), "-i", str(overlay_mov)]
    if has_voice:
        inputs += ["-i", str(voice_track)]

    if band is not None:
        # boxblur radius must fit the smallest plane it touches. For yuv420p the
        # chroma plane is half-res, so its max radius is ~band_h/4 (luma allows
        # ~band_h/2). A fixed radius crashed on short bands (74px -> chroma cap 18).
        # Clamp luma and chroma radii to their planes so any band height is safe.
        # The radius is specified in OUTPUT pixels (like the font), so scale it into
        # source space: at 4K -> 1080p a 20px output blur needs 40px of source blur.
        blur_src = max(1, round(config.blur / (geometry.scale or 1.0)))
        luma_r = max(1, min(blur_src, band_h // 2))
        chroma_r = max(1, min(blur_src, band_h // 4))
        cover = (
            f"[0:v]split=2[base][t];"
            f"[t]crop=iw:{band_h}:0:{band_y},boxblur={luma_r}:2:{chroma_r}:2,boxblur={luma_r}:2:{chroma_r}:2,eq=brightness=-{config.darken:.2f}[bl];"
            f"[base][bl]overlay=0:{band_y}[cov];"
        )
    else:
        cover = "[0:v]copy[cov];"
    # Order matters: cover the source band in SOURCE space, sharpen + scale to the
    # canonical output size, THEN overlay the output-space caption PNGs. Overlaying
    # before the scale is what shrank captions on 4K sources (measured: 58px -> 29px).
    picture_filters = [part for part in (
        f"cas={config.sharpen:.2f}" if config.sharpen > 0 else "",
        config.scale_filter,
    ) if part] or ["null"]
    video_graph = cover + f"[cov]{','.join(picture_filters)}[base2];[base2][1:v]overlay=0:0[vo]"

    if has_voice:
        from backend.app.services.audio import dub_mix_filter

        audio_graph = ";" + dub_mix_filter("[0:a:0]", "[2:a:0]", "[aout]", config.bed_volume, duck=config.duck, carve_db=config.carve_db)
        amap = ["-map", "[aout]"]
    else:
        audio_graph = ""
        amap = ["-map", "0:a:0?"]

    run_command([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", video_graph + audio_graph,
        "-map", "[vo]", *amap,
        *config.video_codec_args,
        # Fixed audio params so a compilation can concat hardsubs with `-c copy`.
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        str(output),
    ])
    return {"covered": band is not None, "band": band, "caption_cy": geometry.caption_cy, "cues": len(cues)}
