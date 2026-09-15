#!/usr/bin/env python3
"""Re-measure the pipeline's cost constants — evidence, never guesses.

Run this whenever the ASR worker, the agent model, the TTS provider or the machine
changes. It prints the constants currently baked into the code next to freshly
measured ones so a drift is visible. It never edits code: measure, then decide.

Constants under test
  dubbing/coverage.py   FULL_PASS_COST_RATIO   full ASR pass, seconds per video-second
                        GLUE_COST_PER_RANGE    glue path, seconds per uncovered range
                        GLUE_COST_PER_SECOND   glue path, seconds per glued speech second
  services/hardsub.py   encoder choice         libx264 vs VideoToolbox (time + SSIM)
  dubbing/translate.py  session capacity       units per agent session within the budget

Usage
  .venv/bin/python scripts/measure_pipeline_costs.py --current
  .venv/bin/python scripts/measure_pipeline_costs.py --encode data/videos/x.mp4
  .venv/bin/python scripts/measure_pipeline_costs.py --asr-full data/videos/x.mp4 --seconds 120
  .venv/bin/python scripts/measure_pipeline_costs.py --asr-glue data/videos/x.mp4 --ranges 12
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.core.config import Settings  # noqa: E402
from backend.app.dubbing import coverage  # noqa: E402
from backend.app.services import hardsub  # noqa: E402


def _run(command: list[str], timeout: float = 1800.0) -> float:
    started = time.monotonic()
    subprocess.run(command, check=True, capture_output=True, timeout=timeout)
    return round(time.monotonic() - started, 1)


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def cmd_current() -> None:
    print("current constants")
    print(f"  ASR  full pass      {coverage.FULL_PASS_COST_RATIO} s per video-second")
    print(f"  ASR  glue           {coverage.GLUE_COST_PER_RANGE} s/range + {coverage.GLUE_COST_PER_SECOND} s/glued-second")
    print(f"  ASR  min bulk       {coverage.MIN_BULK_SECONDS} s of uncovered speech")
    print(f"  package encoder     {' '.join(hardsub.preferred_video_codec_args())}")


def cmd_encode(video: Path, seconds: float) -> None:
    print(f"encode benchmark: {seconds:.0f}s of {video.name}")
    argsets = {
        "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"],
        "videotoolbox": ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-maxrate", "12M"],
    }
    with tempfile.TemporaryDirectory() as tmp:
        for name, args in argsets.items():
            out = Path(tmp) / f"{name}.mp4"
            elapsed = _run(["ffmpeg", "-y", "-v", "error", "-ss", "600", "-t", str(seconds),
                            "-i", str(video), *args, "-pix_fmt", "yuv420p", "-an", str(out)])
            size_mb = out.stat().st_size / 1048576
            ssim = subprocess.run(
                ["ffmpeg", "-v", "info", "-ss", "600", "-t", str(seconds), "-i", str(video), "-i", str(out),
                 "-lavfi", "ssim", "-f", "null", "-"],
                capture_output=True, text=True,
            ).stderr
            match = re.search(r"SSIM.*All:([0-9.]+)", ssim)
            print(f"  {name:14} {elapsed:6.1f}s ({elapsed / seconds:.2f}x realtime)  "
                  f"size {size_mb:5.1f}MB  SSIM {(match.group(1) if match else 'n/a')}")


def cmd_asr_full(video: Path, seconds: float) -> None:
    settings = Settings()
    with tempfile.TemporaryDirectory() as tmp:
        slice_path = Path(tmp) / "slice.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", "300", "-t", str(seconds), "-i", str(video),
             "-c", "copy", str(slice_path)],
            check=True,
        )
        srt = Path(tmp) / "out.srt"
        elapsed = _run([
            sys.executable, str(settings.scripts_dir / "asr_worker_client.py"), str(slice_path),
            "--worker-url", str(settings.asr_worker_url), "--output", str(srt),
            "--language", "auto", "--model", settings.asr_model,
            "--workers", str(settings.asr_workers), "--chunk-seconds", str(settings.asr_chunk_seconds),
            "--max-chars", "18",
        ])
    per_second = elapsed / seconds
    print(f"ASR full pass on {seconds:.0f}s of audio: {elapsed:.1f}s -> {per_second:.3f} s/video-second")
    print(f"  code says {coverage.FULL_PASS_COST_RATIO} (extrapolated 48-min video: "
          f"{per_second * 2880 / 60:.1f} min)")


def cmd_asr_glue(video: Path, ranges: int) -> None:
    settings = Settings()
    duration = _duration(video)
    # two configurations solve range-cost and per-second-cost in one linear fit
    configs = [(ranges, 5.0), (ranges, 15.0)]
    points: list[tuple[int, float, float]] = []
    for count, length in configs:
        step = duration / (count + 1)
        spans = [(step * (index + 1), step * (index + 1) + length) for index in range(count)]
        started = time.monotonic()
        segments = coverage.transcribe_ranges(
            video, spans, str(settings.asr_worker_url), settings.asr_model,
            workers=settings.asr_workers, chunk_seconds=settings.asr_chunk_seconds,
            scripts_dir=settings.scripts_dir, python_bin=sys.executable,
        )
        elapsed = time.monotonic() - started
        points.append((count, count * length, elapsed))
        print(f"  {count} ranges x {length:.0f}s -> {elapsed:.1f}s ({len(segments)} segments)")
    (n1, s1, t1), (n2, s2, t2) = points
    det = (n1 * s2) - (n2 * s1)
    per_range = per_second = None
    if abs(det) > 1e-6:
        per_range = (t1 * s2 - t2 * s1) / det
        per_second = (t2 * n1 - t1 * n2) / det
    print(f"ASR glue fit: {per_range:.2f} s/range + {per_second:.2f} s/glued-second"
          if per_range is not None else "ASR glue fit: degenerate sample; widen the range sizes")
    print(f"  code says {coverage.GLUE_COST_PER_RANGE} + {coverage.GLUE_COST_PER_SECOND}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--current", action="store_true", help="print the constants in use")
    parser.add_argument("--encode", type=Path, help="encoder benchmark on a slice")
    parser.add_argument("--asr-full", type=Path, help="full ASR pass cost ratio")
    parser.add_argument("--asr-glue", type=Path, help="glue path cost fit")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--ranges", type=int, default=12)
    args = parser.parse_args()
    if args.current or not any((args.encode, args.asr_full, args.asr_glue)):
        cmd_current()
    if args.encode:
        cmd_encode(args.encode, min(args.seconds, 60.0))
    if args.asr_full:
        cmd_asr_full(args.asr_full, args.seconds)
    if args.asr_glue:
        cmd_asr_glue(args.asr_glue, args.ranges)


if __name__ == "__main__":
    main()
