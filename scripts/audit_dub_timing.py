#!/usr/bin/env python3
"""Audit dub timing on finished artifacts — evidence only, never touches the pipeline.

Reads:  subtitles/<job>.speech.tsv, subtitles/<job>.segments.tsv,
        tts/<job>.tts.json, tts/<job>.voice.m4a (optional),
        subtitles/<job>.qa.json (optional), subtitles/<job>.glossary.json (optional)

Reports per job:
  * caption coverage: how many rail captions got a Vietnamese row
  * fill per row: measured voice vs the caption's free time and vs the VAD speech
  * simulated placement with the shipped per-row planner (lead, lateness, pauses,
    overflow) — the numbers the fit loop is graded on
  * real pauses in the produced voice track (ffmpeg silencedetect)
  * subtitle shape: rows, mid-sentence rows, overlong cues, glossary variants

Usage: .venv/bin/python scripts/audit_dub_timing.py <job_id> [<job_id> ...]
Writes data/audit/<job>.json and prints a summary table.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
AUDIBLE_GAP = 0.15

sys.path.insert(0, str(ROOT))
from backend.app.dubbing.models import AsrSegment, SpeechRow  # noqa: E402
from backend.app.dubbing.onset import _speech_probs  # noqa: E402
from backend.app.dubbing.coverage import speech_seconds  # noqa: E402
from backend.app.dubbing.timeline import TimelineConfig, plan_rows, summarize  # noqa: E402


def read_rows(job: str) -> list[SpeechRow]:
    path = DATA / "subtitles" / f"{job}.speech.tsv"
    return [SpeechRow.from_row(r) for r in csv.DictReader(path.open(), delimiter="\t")]


def read_segments(job: str) -> list[AsrSegment]:
    path = DATA / "subtitles" / f"{job}.segments.tsv"
    return [AsrSegment.from_row(r) for r in csv.DictReader(path.open(), delimiter="\t")]


def read_timings(job: str) -> dict[int, dict]:
    data = json.loads((DATA / "tts" / f"{job}.tts.json").read_text())
    # new jobs key by sub_id; old artifacts keyed by anchor_id must not crash the audit
    return {int(t.get("sub_id", t.get("anchor_id", -1))): t for t in data.get("timings", [])}


def silence_events(voice: Path) -> list[dict]:
    if not voice.exists():
        return []
    proc = subprocess.run(
        ["ffmpeg", "-i", str(voice), "-af", "silencedetect=noise=-35dB:d=0.20", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    events, current = [], {}
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m:
            current = {"start": float(m.group(1))}
        m = re.search(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", line)
        if m and current:
            current.update(end=float(m.group(1)), duration=float(m.group(2)))
            events.append(current)
            current = {}
    return events


def audit_job(job: str) -> dict:
    rows = read_rows(job)
    segments = read_segments(job)
    timings = read_timings(job)
    report: dict = {"job": job, "rows": len(rows), "captions": len(segments)}

    # 1) caption coverage (every rail caption must have a row on its own window)
    by_caption = {}
    for row in rows:
        lo, _, _hi = row.source_ids.partition("-")
        by_caption[int(lo)] = row if lo.strip().isdigit() else None
    missing = [s.id for s in segments if s.id not in by_caption]
    misaligned = sum(
        1
        for s in segments
        if (row := by_caption.get(s.id)) is not None and (abs(row.anchor_start - s.start) > 0.01 or abs(row.anchor_end - s.end) > 0.01)
    )
    report["coverage"] = {"missing": len(missing), "misaligned": misaligned, "first_missing": missing[:5]}

    # 2) fill per row: measured voice vs free time and vs the source speech (VAD)
    video = DATA / "videos" / f"{job}.mp4"
    packed = _speech_probs(video) if video.exists() else None
    speech_budget = {}
    for index, row in enumerate(rows):
        limit = rows[index + 1].anchor_start if index + 1 < len(rows) else row.anchor_end + 0.5
        speech_budget[row.sub_id] = speech_seconds(packed, row.anchor_start - 0.2, max(limit, row.anchor_end))
    measured = {sub_id: t["tts_duration"] for sub_id, t in timings.items()}
    ratios = [measured[s] / speech_budget[s] for s in speech_budget if speech_budget[s] >= 1.0 and s in measured]
    over_budget = [
        s for s, t in timings.items() if t.get("free_after", -1) > 0 and t["tts_duration"] > t["free_after"] + 0.15
    ]
    ratios.sort()
    report["fill"] = {
        "median_voice_over_speech": round(ratios[len(ratios) // 2], 3) if ratios else None,
        "rows_below_70pct": sum(1 for r in ratios if r < 0.7),
        "rows_over_free_time": len(over_budget),
        "max_overflow_ms": max((t.get("overflow_ms", 0) for t in timings.values()), default=0),
    }

    # 3) the shipped planner, re-run on the measured durations (what the fit loop grades)
    plans = plan_rows(rows, measured, TimelineConfig())
    summary = summarize(plans)
    report["timeline"] = summary
    report["timeline"]["audible_pauses_ge_150ms"] = sum(1 for p in plans if p.gap_before > AUDIBLE_GAP)

    # 4) real pauses in the produced voice track
    events = silence_events(DATA / "tts" / f"{job}.voice.m4a")
    long = [e for e in events if e["duration"] >= 0.3]
    report["voice_track"] = {
        "detected_pauses": len(events),
        "pause_seconds_total": round(sum(e["duration"] for e in events), 1),
        "pauses_ge_300ms": len(long),
        "longest_pause_s": round(max((e["duration"] for e in events), default=0), 2),
    }

    # 5) subtitle shape + glossary hygiene
    mid = sum(1 for r in rows if r.text and not r.text.rstrip().endswith((".", "!", "?", "…", ":", '"')))
    overlong = None
    manifest_path = DATA / "jobs" / f"{job}" / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        for stage in manifest.get("stages", []):
            if stage.get("name") == "qa":
                overlong = (stage.get("metrics") or {}).get("overlong_display_cues")
    glossary_path = DATA / "subtitles" / f"{job}.glossary.json"
    variants = 0
    if glossary_path.exists():
        variants = sum(1 for e in json.loads(glossary_path.read_text()) if "/" in e.get("term_vi", ""))
    report["subtitle_shape"] = {
        "rows": len(rows),
        "rows_mid_sentence": mid,
        "overlong_display_cues": overlong,
        "glossary_multi_variant": variants,
    }
    return report


def main() -> None:
    jobs = sys.argv[1:]
    if not jobs:
        print(__doc__)
        sys.exit(1)
    out_dir = DATA / "audit"
    out_dir.mkdir(exist_ok=True)
    for job in jobs:
        report = audit_job(job)
        (out_dir / f"{job}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\n=== {job} ===")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
