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
import tempfile
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
    # Editable per-job overlay (caption position + cover regions). None = the legacy
    # auto behaviour (detect the band, cover it, caption at its centre).
    overlay: object = None
    # Contrast-adaptive sharpening on the picture (NOT the caption overlay, which is
    # already crisp). Douyin sources sit around 1Mbps for 1080p — visibly soft; a
    # light CAS restores perceived detail without unsharp's halos. 0 disables.
    sharpen: float = 0.35


def _detect_band_vision(video: Path) -> tuple[int, int, int, int] | None:
    """macOS Vision band finder — the SAME detector the transcript rail uses.

    Measured divergence: on a real episode Vision found the band at (893, 144, 1920,
    1080) and 159 captions, while the pixel heuristic below returned None — so the
    package left the original Chinese subtitles fully visible. One detector for both
    jobs, with the pixel heuristic only as the non-macOS fallback."""
    try:
        from backend.app.dubbing.ocr_subs import detect_band as vision_band
    except Exception:  # noqa: BLE001 - absent on non-macOS / missing deps
        return None
    try:
        return vision_band(video)
    except Exception:  # noqa: BLE001 - Vision hiccups must not break packaging
        return None


def detect_sub_band(video: Path, config: HardsubConfig) -> tuple[int, int, int, int] | None:
    """Return (band_y, band_h, width, height) of the original subtitle, or None.

    Vision first — the same detector the transcript rail uses — then the cv2 heuristic
    for non-macOS. The pixel heuristic alone missed a real episode's band (159 captions
    were OCR'd from it), which left the original subtitles uncovered."""
    vision = _detect_band_vision(video)
    if vision is not None:
        return vision
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


def render_caption_png(text: str, path: Path, width: int, height: int, baseline_y: int, font, config: HardsubConfig) -> None:
    """One full-frame transparent PNG with the caption on its fixed baseline."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((width // 2, baseline_y), text, font=font, anchor="ms",
              fill=(255, 255, 255, 255),
              stroke_width=max(2, round(config.font_size * 0.05)),
              stroke_fill=(0, 0, 0, 255))
    img.save(path)


def render_overlay_frame(video: Path, output: Path, t: float, config: HardsubConfig, caption: str = "") -> None:
    """Render ONE frame with the cover regions + caption applied (editor preview).

    Identical chain and space as the real render — scale/pad to the canonical output,
    then cover + caption in OUTPUT coordinates — so the editor's frame is exactly what
    the package will burn (this is what makes the preview trustworthy)."""
    import cv2
    from PIL import ImageFont

    cap = cv2.VideoCapture(str(video))
    src_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
    src_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    cap.release()
    band = detect_sub_band(video, config)
    geometry = overlay_geometry(src_width, src_height, band, config)
    fraction = config.overlay.caption_cy_fraction() if getattr(config, "overlay", None) is not None else None
    caption_cy = round(fraction * geometry.out_height) if fraction is not None else geometry.caption_cy

    regions = _cover_regions(band, config, geometry.out_width, geometry.out_height, geometry)
    cover = _build_cover_chain(regions, config)
    filters = [part for part in (
        config.scale_filter,
        f"cas={config.sharpen:.2f}" if config.sharpen > 0 else "",
    ) if part] or ["null"]
    graph = f"[0:v]{','.join(filters)}[scaled];" + cover.replace("[0:v]", "[scaled]")

    inputs = ["-ss", f"{max(0.0, t):.3f}", "-i", str(video)]
    if caption.strip():
        with tempfile.TemporaryDirectory() as tmp:
            font = ImageFont.truetype(config.font_path, config.font_size)
            ascent, descent = font.getmetrics()
            png = Path(tmp) / "caption.png"
            render_caption_png(caption, png, geometry.out_width, geometry.out_height,
                               caption_cy + (ascent - descent) // 2, font, config)
            graph += "[cov][1:v]overlay=0:0[vo]"
            run_command([
                "ffmpeg", "-y", "-v", "error", *inputs, "-i", str(png),
                "-filter_complex", graph, "-map", "[vo]", "-frames:v", "1", "-q:v", "3", str(output),
            ])
        return
    run_command([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", graph, "-map", "[cov]", "-frames:v", "1", "-q:v", "3", str(output),
    ])


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
        render_caption_png(text, path, width, height, baseline_y, font, config)

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


def _cover_regions(band, config: HardsubConfig, out_width: int, out_height: int, geom: "OverlayGeometry | None" = None) -> list[tuple[int, int, int, int, str, float]]:
    """Cover regions in OUTPUT pixels: (x, y, w, h, mode, strength).

    The cover lives in the SAME space as the caption (the canonical output frame), so
    an editor preview of the output cannot disagree with the render — for a portrait
    source the padding is part of the frame and boxes are positioned inside it, exactly
    as the UI draws them. The editable overlay wins; otherwise the legacy detected band
    is mapped from source pixels into the output frame."""
    overlay = getattr(config, "overlay", None)
    if overlay is not None:
        regions = []
        for region in overlay.cover:
            x = int(region.x * out_width)
            y = int(region.y * out_height)
            w = max(2, int(region.w * out_width)) - (int(region.w * out_width) % 2)
            h = max(2, int(region.h * out_height)) - (int(region.h * out_height) % 2)
            regions.append((x, y, min(w, out_width - x), min(h, out_height - y), region.mode, region.strength))
        return regions
    if band is not None and geom is not None:
        scale = geom.scale or 1.0
        by = max(0, int(band[0] * scale) + geom.pad_y)
        bh = max(2, int(band[1] * scale))
        return [(0, by, out_width, min(bh, out_height - by), "blur", 1.0)]
    if band is not None:
        return [(0, band[0], out_width, band[1], "blur", 1.0)]
    return []


def _build_cover_chain(regions: list[tuple[int, int, int, int, str, float]], config: HardsubConfig) -> str:
    """ffmpeg chain: apply every cover region to the incoming frame in turn."""
    if not regions:
        return "[0:v]copy[cov];"
    parts = ["[0:v]null[v0]"]
    for index, (x, y, w, h, mode, strength) in enumerate(regions):
        label = f"v{index + 1}"
        if mode == "solid":
            box = f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black@1.0:t=fill"
            parts.append(f"[v{index}]{box}[{label}]")
            continue
        blur_src = max(1, round(config.blur * max(0.2, strength)))
        # yuv420p planes: luma allows ~h/2, chroma (half-res) ~h/4. Clamp to both so a
        # short band can never crash the filter graph (74px band -> chroma cap 18).
        luma_r = max(1, min(blur_src, h // 2))
        chroma_r = max(1, min(blur_src, h // 4))
        if mode == "darken":
            box = f"boxblur={luma_r}:2:{chroma_r}:2,boxblur={luma_r}:2:{chroma_r}:2,eq=brightness=-{min(1.0, config.darken * strength):.2f}"
        else:
            box = f"boxblur={luma_r}:2:{chroma_r}:2,boxblur={luma_r}:2:{chroma_r}:2,eq=brightness=-{min(1.0, config.darken * 0.35 * strength):.2f}"
        parts.append(
            f"[v{index}]split=2[b{index}][t{index}];"
            f"[t{index}]crop={w}:{h}:{x}:{y},{box}[c{index}];"
            f"[b{index}][c{index}]overlay={x}:{y}[{label}]"
        )
    return ";".join(parts) + ";[v" + str(len(regions)) + "]null[cov];"


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

    # Editable overlay: draw the caption where the user put it, independent of the
    # detector. Fractions -> output pixels (config.out_* or the source size).
    overlay_caption_cy: int | None = None
    if config.overlay is not None:
        fraction = config.overlay.caption_cy_fraction()
        if fraction is not None:
            overlay_caption_cy = round(fraction * geometry.out_height)

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
    caption_cy = overlay_caption_cy if overlay_caption_cy is not None else geometry.caption_cy
    _build_overlay_video(cues, work_dir / "cues", geometry.out_width, geometry.out_height, caption_cy, overlay_mov, config)

    has_voice = voice_track is not None and Path(voice_track).exists()
    inputs = ["-i", str(video), "-i", str(overlay_mov)]
    if has_voice:
        inputs += ["-i", str(voice_track)]

    regions = _cover_regions(band, config, geometry.out_width, geometry.out_height, geometry)
    cover = _build_cover_chain(regions, config)
    # Order: scale/pad to the canonical OUTPUT first, then cover + caption in that space.
    # Everything the editor draws is output-space, so what it shows is what gets burned
    # (and a 4K source can no longer shrink the caption — measured 58px -> 29px before).
    picture_filters = [part for part in (
        config.scale_filter,
        f"cas={config.sharpen:.2f}" if config.sharpen > 0 else "",
    ) if part] or ["null"]
    video_graph = f"[0:v]{','.join(picture_filters)}[scaled];" + cover.replace("[0:v]", "[scaled]") + f"[cov][1:v]overlay=0:0[vo]"

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
