#!/usr/bin/env python3
"""Mix audition — same 45s excerpt, several dub-mix techniques, so a human picks.

Current pipeline: constant bed at 18% (original audio always quiet, no duck) —
the original voice ghosts under the dub and SFX/music stay buried. Standard
dubbing practice instead: keep the bed loud and DUCK it under narration with a
sidechain compressor, plus a 2-3 dB dip around 2.5 kHz so the Vietnamese voice
cuts through at a lower duck depth, and let music/SFX breathe back in the gaps.

Usage: .venv/bin/python scripts/audition_mix.py <job_id> [start_seconds] [seconds]
"""

from __future__ import annotations

import html
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "auditions_mix"

BED_CARVE = "equalizer=f=2500:t=q:w=1.0:g=-3"
DUCK = "sidechaincompress=threshold=0.03:ratio=6:attack=25:release=420:makeup=1"

VARIANTS = [
    ("current: bed 0.18 constant (no duck, no carve)", "constant", 0.18),
    ("ducked 0.55 -> sidechain 6:1 + carve 2.5k", "ducked_carve", 0.55),
    ("ducked 0.55 sidechain only (no carve)", "ducked", 0.55),
    ("constant bed 0.35", "constant", 0.35),
]


def render(video: Path, voice: Path, out: Path, mode: str, bed_volume: float, start: float, seconds: float) -> None:
    common = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(seconds), "-i", str(video), "-i", str(voice)]
    tail = ["-map", "0:v:0", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)]
    if mode == "constant":
        graph = (
            f"[0:a:0]aresample=44100,volume={bed_volume:.3f}[bed];"
            f"[1:a:0]aresample=44100[voc];"
            f"[bed][voc]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,volume=0.95,alimiter=limit=0.95[aout]"
        )
    else:
        bed_chain = f"[0:a:0]aresample=44100,volume={bed_volume:.3f}"
        if mode == "ducked_carve":
            bed_chain += f",{BED_CARVE}"
        graph = (
            f"{bed_chain}[bed];"
            f"[1:a:0]aresample=44100,volume=1.0,asplit=2[voc][sc];"
            f"[bed][sc]{DUCK}[bedduck];"
            f"[bedduck][voc]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,volume=0.95,alimiter=limit=0.95[aout]"
        )
    subprocess.run(common + ["-filter_complex", graph] + tail, check=True)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    job = sys.argv[1]
    start = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 45.0

    video = next((DATA / "videos").glob(f"{job}*.mp4"), None)
    voice = DATA / "tts" / f"{job}.voice.m4a"
    if video is None or not voice.exists():
        raise SystemExit(f"missing video ({video}) or voice ({voice})")

    OUT.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, (label, mode, bed) in enumerate(VARIANTS):
        out = OUT / f"{index:02d}.mp4"
        render(video, voice, out, mode, bed, start, seconds)
        entries.append({"label": label, "file": out.name})
        print(f"ok [{label}] -> {out.name}")

    players = "\n".join(
        f'<li style="margin:18px 0"><p><b>{html.escape(e["label"])}</b></p>'
        f'<video controls preload="none" width="640" src="{html.escape(e["file"])}"></video></li>'
        for e in entries
    )
    (OUT / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Mix audition</title>"
        "<body style='background:#101716;color:#e9f1ed;font:14px system-ui;padding:20px'>"
        f"<h2>Mix audition — job {html.escape(job)} ({start:.0f}s–{start + seconds:.0f}s)</h2>"
        f"<ol>{players}</ol></body>",
        encoding="utf-8",
    )
    print(OUT / "index.html")


if __name__ == "__main__":
    main()
