"""Smart cover: blur the original subtitle only WHILE it is on screen and only AROUND it.

A plain cover region blurs its box for the whole video — a smeared strip that never
moves is exactly what gives a reupload away. A "smart" region instead:

  1. Finds when and where caption text sits inside the box with the same text-mask the
     OCR rail uses (bright pixels next to a dark outline) — no OCR needed, the cover only
     has to know "is there text, and which columns", not what it says. One cheap decode
     pass over the box, run at render time, so old jobs and LLM-read jobs work too.
  2. Renders a gray mask video: a feathered rectangle around each caption's columns for
     the span it is shown, black in between, with a short linear fade (tmix).
  3. The render alpha-merges the blurred (and lightly grained) box through that mask.

Detection runs in OUTPUT space (after the canonical scale/pad and the mirror flip) so its
columns are the editor box's own, but on the SOURCE timeline: the caller maps spans
through the episode's TimeWarp. Every tolerance errs toward covering more/longer —
a smear that lingers a beat is invisible next to a Chinese line that flashes through.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from backend.app.dubbing.ocr_subs import text_mask
from backend.app.infrastructure.process import ProcessError
from backend.app.services.audio import run_command

if TYPE_CHECKING:
    import numpy as np


@dataclass(slots=True)
class SmartCoverConfig:
    fps: int = 10                 # detection rate: decoding dominates, extra frames are ~free
    # A frame "has text" when the text pixels in the middle of the box (captions are
    # centred; a corner watermark or the frame edge is not) reach this many box-heights
    # squared. Measured on real 1080p bands (h 106-122): a one-glyph caption ("去") gives
    # ~0.03 h2, text-free frames 0-0.001 h2 in the middle but up to 0.02 h2 over the full
    # width — counting the whole box at 0.05 h2 let that one-glyph caption through.
    min_text_h2: float = 0.015
    center: tuple[float, float] = (0.18, 0.82)
    col_min_px: int = 2           # a column holds text when at least this many mask pixels
    col_presence: float = 0.3     # ...in at least this share of the span's frames
    merge_gap: float = 0.3        # gaps shorter than this (s) are one caption flickering
    min_span: float = 0.2         # shorter runs are noise
    # The cover reaches full strength late by the tmix fade (~0.1 s), the mask's ms
    # timestamps (one picture frame) and detection sampling (up to 1/fps): measured, a
    # caption's first frame was still readable at 0.15 s and fully covered at 0.25 s.
    pre_roll: float = 0.3         # cover this long before the text appears
    post_roll: float = 0.2        # ...and after it is gone
    # Measured: consecutive captions often sit 0.05-0.15 s apart; switching the blur off
    # for that long reads as a flicker, far more visible than a blur that stays on.
    min_clear: float = 0.5        # gaps between cover spans shorter than this are bridged
    x_pad_frac: float = 0.5       # widen each caption's columns by this many box heights
    # Captions are centred: only the run of text columns reaching this middle stretch of
    # the box is the caption (glyph gaps under cluster_gap box heights are bridged). A
    # corner watermark is a separate run — measured on a mirrored bilibili logo, letting
    # it widen the cover blurred half of it, which looked worse than leaving it whole.
    anchor: tuple[float, float] = (0.35, 0.65)
    cluster_gap: float = 1.0
    feather_px: int = 14          # soft edge of the mask
    fade_ms: int = 120            # blur fades in/out over this long
    grain: int = 5                # temporal noise on the blur so it keeps the source's texture
    mask_fps: int = 30


@dataclass(slots=True)
class TextSpan:
    start: float  # seconds (source timeline from detect_text_spans)
    end: float
    x0: int       # columns inside the box, x1 exclusive
    x1: int


@dataclass(slots=True)
class _Run:
    start: int                  # frame indices, end exclusive
    end: int
    cols: "np.ndarray"          # per column: frames in which it held text


def _frame_hits(gray: "np.ndarray", config: SmartCoverConfig) -> tuple[bool, "np.ndarray"]:
    """Whether this crop shows caption text, and which columns hold it."""
    mask = text_mask(gray)
    height, width = gray.shape
    middle = mask[:, int(width * config.center[0]):int(width * config.center[1])]
    has_text = int(middle.sum()) >= config.min_text_h2 * height * height
    return has_text, mask.sum(axis=0) >= config.col_min_px


def _extent(cols: "np.ndarray", frames: int, box_w: int, box_h: int, config: SmartCoverConfig) -> tuple[int, int]:
    """The caption's column range, widened by ``x_pad_frac`` box heights. Columns lit
    in only a few frames (a passing highlight) don't widen it, nor does steady text
    apart from the centred caption (see ``anchor``); no steady column at all -> the
    whole box (cover too much rather than too little)."""
    import numpy as np

    steady = np.nonzero(cols >= max(1, config.col_presence * frames))[0]
    if len(steady) == 0:
        return 0, box_w
    clusters = np.split(steady, np.nonzero(np.diff(steady) > config.cluster_gap * box_h)[0] + 1)
    lo, hi = box_w * config.anchor[0], box_w * config.anchor[1]
    centred = [cluster for cluster in clusters if cluster[0] <= hi and cluster[-1] >= lo]
    if centred:
        steady = np.concatenate(centred)
    pad = round(config.x_pad_frac * box_h)
    return max(0, int(steady[0]) - pad), min(box_w, int(steady[-1]) + 1 + pad)


def _smooth(runs: list[_Run], frame_count: int, box_w: int, box_h: int, config: SmartCoverConfig) -> list[TextSpan]:
    """Raw text runs -> cover spans: merge flicker gaps, drop noise, add the rolls."""
    merged: list[_Run] = []
    gap_frames = config.merge_gap * config.fps
    for run in runs:
        if merged and run.start - merged[-1].end < gap_frames:
            last = merged[-1]
            merged[-1] = _Run(last.start, run.end, last.cols + run.cols)
        else:
            merged.append(run)
    duration = frame_count / config.fps
    spans: list[TextSpan] = []
    for run in merged:
        if (run.end - run.start) / config.fps < config.min_span:
            continue
        x0, x1 = _extent(run.cols, run.end - run.start, box_w, box_h, config)
        start = max(0.0, run.start / config.fps - config.pre_roll)
        end = min(duration, run.end / config.fps + config.post_roll)
        if spans and start - spans[-1].end < config.min_clear:
            last = spans[-1]
            spans[-1] = TextSpan(last.start, end, min(last.x0, x0), max(last.x1, x1))
        else:
            spans.append(TextSpan(round(start, 3), round(end, 3), x0, x1))
    return spans


def _crop_filters(picture_filters: list[str], box: tuple[int, int, int, int]) -> str:
    x, y, w, h = box
    return ",".join([*(f for f in picture_filters if f), f"crop={w}:{h}:{x}:{y}", "format=gray"])


def detect_text_spans(
    video: Path,
    box: tuple[int, int, int, int],
    picture_filters: list[str],
    config: SmartCoverConfig | None = None,
    cancel: threading.Event | None = None,
) -> list[TextSpan]:
    """When (source seconds) and where (box columns) caption text shows inside ``box``.

    ``box`` is (x, y, w, h) in the frame produced by ``picture_filters`` (the render's
    scale/pad/mirror). Frames are streamed and dropped (an hour of 1920-wide box at
    10 fps would not fit in memory). Raises on a failed decode; the caller falls back
    to a plain blur."""
    import numpy as np

    config = config or SmartCoverConfig()
    _x, _y, width, height = box
    frame_bytes = width * height
    runs: list[_Run] = []
    current: _Run | None = None
    n = 0
    partial = False
    with subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(video),
         "-vf", f"{_crop_filters(picture_filters, box)},fps={config.fps}:start_time=0",
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    ) as proc:
        try:
            while chunk := proc.stdout.read(frame_bytes):
                if cancel is not None and cancel.is_set():
                    raise ProcessError("canceled: smart-cover detection")
                if len(chunk) < frame_bytes:
                    partial = True
                    break
                has_text, cols = _frame_hits(np.frombuffer(chunk, dtype=np.uint8).reshape(height, width), config)
                if has_text:
                    if current is None:
                        current = _Run(n, n + 1, np.zeros(width, dtype=np.int32))
                    current.end = n + 1
                    current.cols += cols
                elif current is not None:
                    runs.append(current)
                    current = None
                n += 1
        except BaseException:
            proc.kill()  # don't leave ffmpeg blocked on a full pipe
            raise
    if proc.returncode != 0 or partial:
        raise RuntimeError(f"smart-cover: decoding {video.name} failed (exit {proc.returncode}, partial={partial})")
    if current is not None:
        runs.append(current)
    return _smooth(runs, n, width, height, config)


def frame_text_extent(
    video: Path,
    t: float,
    box: tuple[int, int, int, int],
    picture_filters: list[str],
    config: SmartCoverConfig | None = None,
) -> tuple[int, int] | None:
    """The caption's columns in ``box`` on the frame at ``t``, or None when no text
    shows there (the editor preview of a smart region). Raises when the frame cannot
    be read, so the caller can show the plain-blur fallback the render would use."""
    import numpy as np

    config = config or SmartCoverConfig()
    _x, _y, width, height = box
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
         "-vf", _crop_filters(picture_filters, box), "-frames:v", "1", "-f", "rawvideo", "-"],
        capture_output=True, check=False, stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0 or len(completed.stdout) < width * height:
        raise RuntimeError(f"smart-cover: no {width}x{height} frame at {t:.2f}s of {video.name}")
    gray = np.frombuffer(completed.stdout[:width * height], dtype=np.uint8).reshape(height, width)
    has_text, cols = _frame_hits(gray, config)
    return _extent(cols.astype(np.int32), 1, width, height, config) if has_text else None


def mask_png(extent: tuple[int, int] | None, box_w: int, box_h: int, config: SmartCoverConfig | None = None):
    """The gray mask for one caption: white over its (widened) columns with a feathered
    edge, black elsewhere; ``None`` extent = all black. Vertically the white core stops
    half a feather short of the box edges so the blur also fades out top and bottom."""
    from PIL import Image, ImageDraw, ImageFilter

    config = config or SmartCoverConfig()
    image = Image.new("L", (box_w, box_h), 0)
    if extent is None:
        return image
    feather = max(0, min(config.feather_px, box_h // 6))
    inset = feather // 2
    ImageDraw.Draw(image).rectangle((extent[0], inset, extent[1] - 1, box_h - 1 - inset), fill=255)
    return image.filter(ImageFilter.GaussianBlur(feather / 2)) if feather else image


def build_mask_video(
    spans: list[TextSpan],
    box_w: int,
    box_h: int,
    total_seconds: float,
    work_dir: Path,
    output: Path,
    config: SmartCoverConfig | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """One gray mask video for a smart region: each span's mask held for its duration
    (spans in OUTPUT seconds), black between, at least ``total_seconds + 1`` long so the
    alpha merge never runs out of mask before the picture ends. ``tmix`` over
    ``fade_ms`` turns every on/off cut into a linear ramp."""
    config = config or SmartCoverConfig()
    work_dir.mkdir(parents=True, exist_ok=True)
    blank = work_dir / "blank.png"
    mask_png(None, box_w, box_h, config).save(blank)
    entries: list[tuple[Path, float]] = []
    cursor = 0.0
    for index, span in enumerate(spans):
        if span.end <= cursor:
            continue
        start = max(cursor, span.start)
        if start > cursor:
            entries.append((blank, start - cursor))
        png = work_dir / f"m{index:05d}.png"
        mask_png((span.x0, span.x1), box_w, box_h, config).save(png)
        entries.append((png, span.end - start))
        cursor = span.end
    entries.append((blank, max(1.0, total_seconds + 1.0 - cursor)))
    concat = work_dir / "concat.txt"
    with concat.open("w", encoding="utf-8") as handle:
        for path, duration in entries:
            handle.write(f"file '{path.resolve().as_posix()}'\nduration {duration:.3f}\n")
        handle.write(f"file '{entries[-1][0].resolve().as_posix()}'\n")
    frames = max(1, round(config.fade_ms / 1000 * config.mask_fps))
    fade = f",tmix=frames={frames}" if frames > 1 else ""
    run_command([
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
        "-vf", f"fps={config.mask_fps}{fade},format=gray",
        "-c:v", "ffv1", "-pix_fmt", "gray", str(output),
    ], cancel=cancel)
