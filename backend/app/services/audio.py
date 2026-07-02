"""In-process ffmpeg helpers for voice rendering.

Ported from the prototype synth script so the pipeline never shells out to a
``scripts/`` file. Pure ffmpeg/ffprobe mechanics: probe duration, tempo-fit a
clip to a target window, render silence, and concat-assemble a timed track.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
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


def align_audio(
    raw_path: Path, output_path: Path, target_duration: float, min_tempo: float, max_tempo: float, fixed_tempo: float | None = None
) -> dict:
    """Tempo-fit a raw clip toward ``target_duration``.

    With ``fixed_tempo`` set, every clip is played at exactly that tempo — one
    stable speaking rate across the whole dub — and timing is fitted by the text
    repair loop instead of by varying the tempo per phrase. Otherwise the tempo is
    clamped to ``[min_tempo, max_tempo]`` to fit each clip's window.
    """
    raw_duration = probe_duration(raw_path)
    if fixed_tempo:
        tempo = fixed_tempo
    else:
        tempo = clamp(raw_duration / max(0.001, target_duration), min_tempo, max_tempo)
    playback_duration = raw_duration / tempo
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic publish: this output is a SHARED cache entry and concurrent jobs read it.
    # MP4 writes its moov atom last, so a reader catching a direct write mid-encode
    # sees a corrupt file ("moov atom not found"). Encode to a WRITER-UNIQUE temp name
    # and rename — readers only ever see complete files, and two jobs rendering the
    # same entry at once each build their own temp (last rename wins, both valid).
    part = output_path.with_name(f"{output_path.stem}.{os.getpid()}-{threading.get_ident()}.part{output_path.suffix}")
    run_command([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(raw_path),
        "-filter:a", f"aresample=44100,{atempo_chain(tempo)}",
        "-ac", "2", "-c:a", "aac", "-b:a", "192k",
        str(part),
    ])
    part.replace(output_path)
    return {
        "raw_duration": round(raw_duration, 3),
        "target_duration": round(target_duration, 3),
        "tempo": round(tempo, 4),
        "playback_duration": round(playback_duration, 3),
        "timing_status": "fits" if playback_duration <= target_duration + 0.05 else "overflow",
        "overflow_ms": max(0, int(round((playback_duration - target_duration) * 1000))),
    }


# Voice mastering chain ported from the proven probe pipeline — what made the
# old voice-over pleasant to listen to: de-rumble, tame mud at 220 Hz, lift
# presence (3.2 kHz) and air (7.6 kHz), even out levels, then limit.
VOICE_MASTER_CHAIN = (
    "highpass=f=80,"
    "equalizer=f=200:t=q:w=1.0:g=-1.0,"   # gentle low-mud cut
    "equalizer=f=3000:t=q:w=1.4:g=1.0,"   # mild presence (was +2.2 — that harshness read as "rè")
    "lowpass=f=13000,"                      # tame top-end fizz/sibilance + mp3 artifacts
    "acompressor=threshold=-20dB:ratio=2.0:attack=12:release=160:makeup=1.5,"  # gentler, less make-up gain
    "alimiter=limit=0.95"
)


def master_voice(input_path: Path, output_path: Path, pitch: float = 1.0) -> None:
    """Apply the voice mastering chain so the dub is clear and easy to listen to.

    ``pitch`` > 1.0 brightens a too-deep voice WITHOUT changing tempo/duration:
    resample up (raises pitch + speed) then atempo back down by the same factor."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    chain = f"aresample=44100,{VOICE_MASTER_CHAIN}"
    if abs(pitch - 1.0) > 1e-3:
        shift = f"asetrate=44100*{pitch:.4f},aresample=44100,{atempo_chain(1.0 / pitch)}"
        chain = f"{shift},{chain}"
    run_command([
        "ffmpeg", "-y", "-v", "error",
        "-i", str(input_path),
        "-filter:a", chain,
        "-ac", "2", "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ])


def speed_clip(path: Path, rate: float) -> None:
    """Speed up (or slow) a clip in place by ``rate`` via atempo. Needed because the
    CapCut synth IGNORES its ``rate`` parameter (verified: 1.0 and 1.4 give identical
    audio), so the only way to change the speaking rate is to time-stretch locally."""
    if abs(rate - 1.0) < 1e-3:
        return
    tmp = path.with_name(path.stem + ".sp" + path.suffix)
    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-filter:a", atempo_chain(rate), str(tmp)])
    if tmp.exists() and tmp.stat().st_size > 512:
        tmp.replace(path)


def trim_silence(path: Path, threshold_db: float = -45.0, keep: float = 0.03) -> None:
    """Strip leading/trailing silence from a synthesized clip so it starts and ends on
    speech. CapCut adds ~0.1-0.2s of head silence; left in, the clip (placed at the
    source time) and its caption both lead the actual voice by that much. ``keep``
    leaves a hair of padding so the first/last phoneme isn't clipped."""
    tmp = path.with_name(path.stem + ".trim" + path.suffix)
    chain = (
        f"silenceremove=start_periods=1:start_silence={keep}:start_threshold={threshold_db}dB:detection=peak,"
        f"areverse,"
        f"silenceremove=start_periods=1:start_silence={keep}:start_threshold={threshold_db}dB:detection=peak,"
        f"areverse"
    )
    run_command(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-af", chain, str(tmp)])
    if tmp.exists() and tmp.stat().st_size > 512:
        tmp.replace(path)


def render_silence(path: Path, duration: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", f"{max(0.001, duration):.3f}",
        "-c:a", "aac", "-b:a", "192k",
        str(path),
    ])


def plan_placements(
    segments: list[tuple[float, Path, float, float]], glue_gap: float, crossfade: float, max_lead: float = 0.3
) -> tuple[list[tuple[float, Path, float, bool]], float]:
    """Resolve each clip's slot on the output timeline + how it joins the previous.

    ``segments`` is ``(anchor_start, clip_path, playback_duration, anchor_end)`` in
    play order. Returns ``(placements, track_end)`` where each placement is
    ``(place_start, path, duration, glued_head)``:

    * Real pause — ``next.start - this.end >= glue_gap``: the clip keeps its absolute
      onset, so the voice stays locked to the picture ("khớp ảnh") and the space
      before it is a genuine on-screen beat.
    * Contiguous — gap ``< glue_gap``: no real pause on screen, so the clip butt-joins
      right after the previous voice (overlapping by ``crossfade``); the artificial
      underfill silence is dropped so prosody flows with no stop-start "khựng".

    ``max_lead`` bounds how far a glued clip may run AHEAD of its source time: over a
    long continuous run, condensed (shorter) lines pulled every following line earlier
    and the accumulated drift had the dub speaking a full second before the on-screen
    speaker. A clip never starts more than ``max_lead`` before its source onset — the
    leftover space is a short natural breath, not a stall.
    """
    placements: list[tuple[float, Path, float, bool]] = []
    cursor = 0.0
    prev_end: float | None = None
    for start, path, duration, end in segments:
        if prev_end is None:
            place, glued = start, False
        elif start - prev_end < glue_gap:
            place, glued = max(0.0, cursor - crossfade, start - max_lead), True
        else:
            place, glued = max(start, cursor), False
        placements.append((place, path, duration, glued))
        cursor = place + duration
        prev_end = end
    return placements, cursor


def assemble_track(
    segments: list[tuple[float, Path, float, float]],
    output_path: Path,
    work_dir: Path,
    total_duration: float | None = None,
    glue_gap: float = 0.30,
    crossfade: float = 0.06,
    max_lead: float = 0.3,
) -> None:
    """Place tempo-fitted clips on a timeline and smooth the joins.

    One ffmpeg filtergraph: fade each clip's edges (equal-power ``qsin`` where it
    overlaps a glued neighbour, a short declick otherwise), delay it into its slot,
    then ``amix``. amix sums, so the matched crossfades stay at unity while gaps
    between real beats stay silent. Overflow stays local — a long clip never pushes a
    later real-beat onset earlier, only consumes its own trailing silence.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not segments:
        render_silence(output_path, max(0.1, total_duration or 0.1))
        return

    placements, cursor = plan_placements(segments, glue_gap, crossfade, max_lead)
    track_end = max(cursor, total_duration or 0.0)

    declick = 0.008
    n = len(placements)
    inputs: list[str] = []
    chains: list[str] = []
    labels: list[str] = []
    for index, (place, path, duration, glued_head) in enumerate(placements):
        inputs += ["-i", str(path)]
        glued_tail = index + 1 < n and placements[index + 1][3]
        fin = min(crossfade if glued_head else declick, duration / 2)
        fout = min(crossfade if glued_tail else declick, duration / 2)
        fin_curve = "qsin" if glued_head else "tri"
        fout_curve = "qsin" if glued_tail else "tri"
        delay_ms = max(0, int(round(place * 1000)))
        chains.append(
            f"[{index}:a]aresample=44100,"
            f"afade=t=in:curve={fin_curve}:st=0:d={fin:.3f},"
            f"afade=t=out:curve={fout_curve}:st={max(0.0, duration - fout):.3f}:d={fout:.3f},"
            f"adelay={delay_ms}:all=1[a{index}]"
        )
        labels.append(f"[a{index}]")

    if n == 1:
        graph = chains[0] + ";[a0]apad[mix]"
    else:
        graph = ";".join(chains) + ";" + "".join(labels) + f"amix=inputs={n}:normalize=0:dropout_transition=0,apad[mix]"

    run_command([
        "ffmpeg", "-y", "-v", "error",
        *inputs,
        "-filter_complex", graph,
        "-map", "[mix]",
        "-t", f"{max(0.1, track_end):.3f}",
        "-ac", "2", "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ])
