"""Extract the ASR rail directly from a video's burned-in (hardsub) subtitles via
macOS Vision OCR — the primary transcript source for 短剧 (the on-screen text IS the
script; more accurate than ASR and immune to its homophone/proper-noun mistakes), with
Whisper ASR kept as the fallback when a video has no usable hardsub coverage.

Pipeline (measured ~4s for a 106s episode, 24.5x realtime, no re-encode):
  1. One ffmpeg pass decodes+crops ONLY the subtitle band at a low fps (cheap: a few
     hundred KB of gray pixels, not full frames).
  2. A text-mask (bright-next-to-dark-outline) column profile per frame turns "did the
     CAPTION change" into a cheap numpy diff — this ignores background motion/animation,
     which a raw pixel diff does not (that over-segmented ~8x in testing).
  3. OCR runs ONCE per stable caption run (not per frame) — cost scales with the number
     of distinct lines (~25-80/episode), not frame count.
  4. Coarse (1/fps) start times are handed to the existing Silero-VAD ``refine_segments``
     snap, same as the ASR rail — measured: hardsub timing already matches VAD onset for
     ~96% of cues (creators author it against the real voice); only edge cases need the
     snap. This is why no separate "OCR onset" logic is needed here.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import AsrSegment
from .text import clean

Band = tuple[int, int, int, int]  # (band_y, band_h, width, height) — see hardsub.detect_sub_band


@dataclass(slots=True)
class OcrExtractConfig:
    fps: int = 4  # caption sampling rate; timing is later snapped to VAD, so coarse is fine
    # Vision's "accurate" recognizer doesn't give CJK text a continuous confidence score —
    # measured output only ever lands on {0.3, 0.5, 1.0}. 0.3 is a legitimate read, not
    # noise (verified against source frames: full correct sentences, same error rate as
    # the 0.5/1.0 tiers); a 0.5 floor was silently dropping ~25% of real captions with no
    # fallback. Position + width below already reject the actual junk (icon glyphs,
    # split single characters land under 0.05 width).
    min_conf: float = 0.2
    cx_lo: float = 0.25   # a caption box's centre-x must sit in this fraction of the
    cx_hi: float = 0.75   # band width (horizontally centred — excludes corner UI/logos)
    min_width_frac: float = 0.08   # caption box must span at least this much of the band width
    diff_threshold: float = 0.35   # 1-cosine-similarity of column profiles -> "caption changed"
    empty_frac: float = 0.02       # text-pixel fraction below this -> "no caption" frame
    # Vision reads worse when the crop is flush against the glyphs (clips ascenders/
    # descenders, box-fit gets noisier near the edge — measured: several cues only came
    # back correct with this padding). Only the OCR crop gets it; change detection below
    # still runs on the exact original band so cue-run timing is unaffected.
    ocr_pad_frac: float = 0.18


def ocr_coverage(segments: list[AsrSegment], duration: float) -> float:
    """Fraction of the video's runtime covered by OCR-derived speech segments."""
    if duration <= 0:
        return 0.0
    return sum(seg.duration for seg in segments) / duration


def extract_ocr_segments(video: Path, band: Band, config: OcrExtractConfig | None = None) -> list[AsrSegment] | None:
    """OCR the burned-in subtitle track into an ASR-shaped rail.

    Returns ``None`` (never an exception) when OCR can't run at all — Vision
    unavailable, or the ffmpeg decode failed — so the caller falls back to Whisper.
    Returns ``[]`` when OCR ran cleanly but found no captions (a same-condition signal
    as ``None`` for the caller's coverage check, kept distinct for logging clarity)."""
    try:
        from ocrmac import ocrmac
    except Exception:
        return None
    import cv2
    import numpy as np
    from PIL import Image

    config = config or OcrExtractConfig()
    band_y, band_h, width, height = band
    if band_h <= 0 or width <= 0:
        return None

    # Crop wider than the band for OCR's sake (see ocr_pad_frac); change detection
    # below re-slices back to the exact original band so cue-run timing is unaffected.
    pad = max(1, round(config.ocr_pad_frac * band_h))
    crop_y0 = max(0, band_y - pad)
    crop_y1 = min(height, band_y + band_h + pad)
    crop_h = crop_y1 - crop_y0
    band_top = band_y - crop_y0

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video),
         "-vf", f"crop={width}:{crop_h}:0:{crop_y0},fps={config.fps},format=gray",
         "-f", "rawvideo", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    frame_bytes = width * crop_h
    n = len(proc.stdout) // frame_bytes
    if n < 2:
        return None
    frames = np.frombuffer(proc.stdout[: n * frame_bytes], dtype=np.uint8).reshape(n, crop_h, width)
    band_frames = frames[:, band_top:band_top + band_h, :]

    # --- change detection on a text-mask column profile (not raw pixels) ---
    x0, x1 = int(width * 0.18), int(width * 0.82)

    def mask_col(frame: "np.ndarray") -> tuple["np.ndarray", int]:
        bright = (frame >= 195).astype(np.uint8)
        dark = cv2.dilate((frame <= 85).astype(np.uint8), np.ones((5, 5), np.uint8))
        text = (bright & dark)[:, x0:x1]
        return text.sum(axis=0).astype(np.float32), int(text.sum())

    cols = np.empty((n, x1 - x0), dtype=np.float32)
    totals = np.empty(n, dtype=np.float32)
    for i, frame in enumerate(band_frames):
        cols[i], totals[i] = mask_col(frame)

    def normalize(vec: "np.ndarray") -> "np.ndarray":
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    normalized = np.array([normalize(c) for c in cols])
    diff = 1 - np.sum(normalized[1:] * normalized[:-1], axis=1)
    empty = totals < (config.empty_frac * band_h * (x1 - x0))
    changed = (diff > config.diff_threshold) | (empty[1:] != empty[:-1])

    runs: list[tuple[int, int]] = []
    cursor = 0
    for i, is_change in enumerate(changed):
        if is_change:
            runs.append((cursor, i + 1))
            cursor = i + 1
    runs.append((cursor, n))
    cue_runs = [r for r in runs if empty[r[0]:r[1]].mean() <= 0.5]

    # --- OCR one representative frame per stable run ---
    segments: list[AsrSegment] = []
    for start_i, end_i in cue_runs:
        mid = frames[(start_i + end_i) // 2]
        pil = Image.fromarray(cv2.cvtColor(mid, cv2.COLOR_GRAY2RGB))
        try:
            result = ocrmac.OCR(pil, recognition_level="accurate",
                                language_preference=["zh-Hans", "en-US"]).recognize()
        except Exception:
            continue
        # Vision returns one box PER LINE, so a wrapped two-line caption is two
        # candidates here — keep every box that passes the filters (not just the
        # widest) or the second line silently vanished from the transcript. Vision's
        # bbox is bottom-left origin (larger y = higher on screen), so sort descending
        # to read top line first.
        candidates: list[tuple[float, str]] = []
        for candidate_text, conf, (bx, by, bw, _bh) in result or []:
            cx = bx + bw / 2
            if conf >= config.min_conf and config.cx_lo < cx < config.cx_hi and bw > config.min_width_frac:
                candidates.append((by, candidate_text.strip()))
        candidates.sort(key=lambda c: -c[0])
        text = clean("".join(part for _, part in candidates if part))
        if text:
            segments.append(AsrSegment(
                id=len(segments) + 1,
                start=round(start_i / config.fps, 3),
                end=round(end_i / config.fps, 3),
                text_zh=text,
            ))
    return segments


def detect_band(video: Path, frames: int = 12, crop_top: float = 0.55, pad_frac: float = 0.01) -> Band | None:
    """Locate the burned-in subtitle band via macOS Vision OCR.

    Samples the bottom strip, keeps bottom/horizontally-centred/wide text boxes (the
    caption, not watermarks or corner UI) and returns their aggregated y-range.
    Returns None when Vision is unavailable or too few sampled frames carry a caption
    (the caller then runs ASR only)."""
    try:
        from ocrmac import ocrmac
    except Exception:
        return None
    import cv2
    import numpy as np
    from PIL import Image

    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width == 0 or height == 0:
        cap.release()
        return None
    crop_y0 = int(max(0.0, min(0.9, crop_top)) * height)
    crop_h = height - crop_y0
    tops: list[float] = []
    bottoms: list[float] = []
    hits = 0
    for index in range(max(6, frames)):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (index + 0.5) / max(6, frames)))
        ok, frame = cap.read()
        if not ok:
            continue
        crop = frame[crop_y0:, :]
        pil = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        try:
            result = ocrmac.OCR(pil, recognition_level="accurate", language_preference=["zh-Hans", "en-US"]).recognize()
        except Exception:
            continue
        found = False
        for _text, conf, (bx, by, bw, bh) in result or []:
            cx = bx + bw / 2
            if conf >= 0.5 and 0.25 < cx < 0.75 and bw > 0.12:
                tops.append(crop_y0 + (1 - (by + bh)) * crop_h)
                bottoms.append(crop_y0 + (1 - by) * crop_h)
                found = True
        hits += found
    cap.release()
    if hits < max(6, frames) * 0.25 or not tops:
        return None
    pad = max(1, round(pad_frac * height))
    band_y = max(0, int(np.percentile(tops, 10)) - pad)
    band_bottom = min(height, int(np.percentile(bottoms, 90)) + pad)
    if band_bottom - band_y < height * 0.02:
        return None
    return band_y, band_bottom - band_y, width, height
