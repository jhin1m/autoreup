"""In-process ffmpeg helpers for voice rendering.

Ported from the prototype synth script so the pipeline never shells out to a
``scripts/`` file. Pure ffmpeg/ffprobe mechanics: probe duration, tempo-fit a
clip to a target window, render silence, and concat-assemble a timed track.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def run_command(args: list[str]) -> list[str]:
    completed = subprocess.run(args, text=True, capture_output=True, check=False)
    logs: list[str] = []
    if completed.stdout.strip():
        logs.extend(completed.stdout.strip().splitlines())
    if completed.stderr.strip():
        logs.extend(completed.stderr.strip().splitlines())
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(args)}\n" + "\n".join(logs[-20:]))
    return logs


def require_ffmpeg() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        raise RuntimeError(f"Missing required audio tool(s): {', '.join(missing)}")


def ffmpeg_available() -> bool:
    return not [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def probe_duration(path: Path) -> float:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {completed.stderr.strip()}")
    return max(0.001, float(completed.stdout.strip()))


def atempo_chain(tempo: float) -> str:
    """ffmpeg atempo only accepts 0.5-2.0; chain factors to reach any tempo."""
    parts: list[float] = []
    tempo = max(0.001, tempo)
    while tempo > 2.0:
        parts.append(2.0)
        tempo /= 2.0
    while tempo < 0.5:
        parts.append(0.5)
        tempo /= 0.5
    parts.append(tempo)
    return ",".join(f"atempo={part:.6f}" for part in parts)


def align_audio(raw_path: Path, output_path: Path, target_duration: float, min_tempo: float, max_tempo: float) -> dict:
    """Tempo-fit a raw clip toward ``target_duration`` within tempo bounds."""
    raw_duration = probe_duration(raw_path)
    tempo = clamp(raw_duration / max(0.001, target_duration), min_tempo, max_tempo)
    playback_duration = raw_duration / tempo
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(raw_path),
        "-filter:a", f"aresample=44100,{atempo_chain(tempo)}",
        "-ac", "2", "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ])
    return {
        "raw_duration": round(raw_duration, 3),
        "target_duration": round(target_duration, 3),
        "tempo": round(tempo, 4),
        "playback_duration": round(playback_duration, 3),
        "timing_status": "fits" if playback_duration <= target_duration + 0.05 else "overflow",
        "overflow_ms": max(0, int(round((playback_duration - target_duration) * 1000))),
    }


def render_silence(path: Path, duration: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", f"{max(0.001, duration):.3f}",
        "-c:a", "aac", "-b:a", "192k",
        str(path),
    ])


def assemble_track(segments: list[tuple[float, Path]], output_path: Path, work_dir: Path, total_duration: float | None = None) -> None:
    """Concat clips at absolute start times, padding gaps with silence.

    Absolute scheduling: each clip starts at its own ``start``; an overlong clip
    never shifts the next one's start, so overflow stays local to one anchor.
    """
    concat_items: list[Path] = []
    cursor = 0.0
    for index, (start, path) in enumerate(segments):
        gap = start - cursor
        if gap > 0.01:
            silence = work_dir / "silence" / f"gap-{index:05d}.m4a"
            render_silence(silence, gap)
            concat_items.append(silence)
            cursor += gap
        concat_items.append(path)
        cursor += probe_duration(path)
    if total_duration and total_duration - cursor > 0.01:
        silence = work_dir / "silence" / "tail.m4a"
        render_silence(silence, total_duration - cursor)
        concat_items.append(silence)
    if not concat_items:
        render_silence(output_path, max(0.1, total_duration or 0.1))
        return
    list_path = work_dir / "concat.txt"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(
        "\n".join(f"file '{path.resolve().as_posix()}'" for path in concat_items) + "\n", encoding="utf-8"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(output_path)])
