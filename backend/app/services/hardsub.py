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

from dataclasses import dataclass
from pathlib import Path

from backend.app.services.audio import run_command


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
    bed_volume: float = 0.18


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


def _build_overlay_video(cues: list[dict], sub_dir: Path, width: int, height: int, caption_cy: int, output: Path, config: HardsubConfig) -> None:
    """Render each cue to a transparent full-frame PNG and concat (held for each cue's
    duration, with transparent gaps) into one alpha overlay video."""
    from PIL import Image, ImageDraw, ImageFont

    sub_dir.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(config.font_path, config.font_size)
    blank = sub_dir / "blank.png"
    Image.new("RGBA", (width, height), (0, 0, 0, 0)).save(blank)

    def render(text: str, path: Path) -> None:
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=3)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((width - tw) // 2 - bbox[0], caption_cy - th // 2 - bbox[1]), text,
                  font=font, fill=(255, 255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0, 255))
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

    band = detect_sub_band(video, config)
    if band is not None:
        band_y, band_h, width, height = band
        caption_cy = band_y + band_h // 2          # VI sits where the original was
    else:
        import cv2
        cap = cv2.VideoCapture(str(video))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
        cap.release()
        caption_cy = height - config.margin_v

    overlay_mov = work_dir / "vi_overlay.mov"
    _build_overlay_video(cues, work_dir / "cues", width, height, caption_cy, overlay_mov, config)

    has_voice = voice_track is not None and Path(voice_track).exists()
    inputs = ["-i", str(video), "-i", str(overlay_mov)]
    if has_voice:
        inputs += ["-i", str(voice_track)]

    if band is not None:
        blur = min(23, max(1, config.blur))
        cover = (
            f"[0:v]split=2[base][t];"
            f"[t]crop=iw:{band_h}:0:{band_y},boxblur={blur}:2,boxblur={blur}:2,eq=brightness=-{config.darken:.2f}[bl];"
            f"[base][bl]overlay=0:{band_y}[cov];"
        )
    else:
        cover = "[0:v]copy[cov];"
    video_graph = cover + "[cov][1:v]overlay=0:0[vo]"

    if has_voice:
        audio_graph = (
            f";[0:a:0]aresample=44100,volume={config.bed_volume:.3f}[bed];"
            f"[2:a:0]aresample=44100,volume=1.0[voc];"
            f"[bed][voc]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,volume=0.95,alimiter=limit=0.95[aout]"
        )
        amap = ["-map", "[aout]"]
    else:
        audio_graph = ""
        amap = ["-map", "0:a:0?"]

    run_command([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", video_graph + audio_graph,
        "-map", "[vo]", *amap,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        str(output),
    ])
    return {"covered": band is not None, "band": band, "caption_cy": caption_cy, "cues": len(cues)}
