#!/usr/bin/env python3
"""Audit dub timing on finished artifacts — evidence only, never touches the pipeline.

Reads:  subtitles/<job>.anchors.tsv, subtitles/<job>.speech.tsv,
        tts/<job>.tts.json, tts/<job>.voice.m4a (optional),
        subtitles/<job>.qa.json (optional), subtitles/<job>.glossary.json (optional)

Reports per job:
  * underfill per anchor (target vs tts duration) — how often the dub undershoots
  * source pause histogram — validates the run-gap threshold
  * speech runs: voice total vs run duration ("dead air inside runs")
  * simulated placement with the CURRENT scheduler (glue_gap/max_lead) —
    artificial intra-run silence, early-lead drift
  * real pauses in the produced voice track (ffmpeg silencedetect)
  * subtitle shape: rows/anchor, mid-sentence rows, overlong cues, glossary variants

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
RUN_GAP = 0.8          # a source pause below this is "same speech run"
GLUE_GAP = 0.30        # current scheduler: below this = glued (no absolute onset)
MAX_LEAD = 0.3         # current scheduler: never start earlier than source - this
CROSSFADE = 0.06
AUDIBLE_GAP = 0.15     # a silence this long inside a run is audible choppiness

sys.path.insert(0, str(ROOT))
from backend.app.services.audio import plan_placements  # noqa: E402


def read_anchors(job: str) -> list[dict]:
    path = DATA / "subtitles" / f"{job}.anchors.tsv"
    rows = list(csv.DictReader(path.open(), delimiter="\t"))
    return [{"id": int(r["ID"]), "start": float(r["START"]), "end": float(r["END"]), "zh": r["SOURCE_ZH"]} for r in rows]


def read_timings(job: str) -> dict[int, dict]:
    data = json.loads((DATA / "tts" / f"{job}.tts.json").read_text())
    return {int(t["anchor_id"]): t for t in data.get("timings", [])}


def group_runs(anchors: list[dict], gap: float = RUN_GAP) -> list[list[dict]]:
    runs: list[list[dict]] = []
    for anchor in anchors:
        if runs and anchor["start"] - runs[-1][-1]["end"] < gap:
            runs[-1].append(anchor)
        else:
            runs.append([anchor])
    return runs


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
    anchors = read_anchors(job)
    timings = read_timings(job)
    report: dict = {"job": job, "anchors": len(anchors)}

    # 1) underfill
    under = [
        (t["target_duration"] - t["tts_duration"]) / max(0.001, t["target_duration"])
        for t in timings.values()
        if t["target_duration"] > 0
    ]
    voice_total = sum(t["tts_duration"] for t in timings.values())
    source_total = sum(t["target_duration"] for t in timings.values())
    under_sorted = sorted(under)
    report["underfill"] = {
        "median": round(under_sorted[len(under) // 2], 3) if under else None,
        "gt_25pct": sum(1 for x in under if x > 0.25),
        "gt_40pct": sum(1 for x in under if x > 0.40),
        "voice_to_source_ratio": round(voice_total / max(0.001, source_total), 3),
    }

    # 2) source pause histogram
    gaps = [b["start"] - a["end"] for a, b in zip(anchors, anchors[1:])]
    report["source_pause_hist"] = {
        "<0.3": sum(1 for g in gaps if g < 0.3),
        "0.3-0.8": sum(1 for g in gaps if 0.3 <= g < 0.8),
        "0.8-1.5": sum(1 for g in gaps if 0.8 <= g < 1.5),
        ">1.5": sum(1 for g in gaps if g >= 1.5),
    }

    # 3) runs: dead air if the whole run were played at source boundaries
    runs = group_runs(anchors)
    run_slack = []
    for run in runs:
        run_dur = run[-1]["end"] - run[0]["start"]
        voice = sum(timings[a["id"]]["tts_duration"] for a in run if a["id"] in timings)
        run_slack.append({"first": run[0]["id"], "last": run[-1]["id"], "run_dur": round(run_dur, 2),
                          "voice": round(voice, 2), "slack_ratio": round((run_dur - voice) / max(0.001, run_dur), 3)})
    worst = sorted(run_slack, key=lambda r: -r["slack_ratio"])[:5]
    report["runs"] = {
        "count": len(runs),
        "silent_ratio": round(1 - voice_total / max(0.001, sum(r["run_dur"] for r in run_slack)), 3),
        "slack_gt_25pct": sum(1 for r in run_slack if r["slack_ratio"] > 0.25),
        "worst": worst,
    }

    # 4) simulated placement with the CURRENT scheduler
    segments = [(a["start"], Path("x"), timings[a["id"]]["tts_duration"], a["end"]) for a in anchors if a["id"] in timings]
    placements, track_end = plan_placements(segments, GLUE_GAP, CROSSFADE, MAX_LEAD)
    intra_silence, early = 0.0, []
    by_id = {a["id"]: a for a in anchors}
    for i in range(1, len(placements)):
        prev_place, _, prev_dur, _ = placements[i - 1]
        place, _, _, glued = placements[i]
        gap = place - (prev_place + prev_dur)
        inside_run = segments[i][0] - segments[i - 1][3] < RUN_GAP
        if inside_run and gap > AUDIBLE_GAP:
            intra_silence += gap
    for (a_start, _, _, _), (place, _, _, _) in zip(segments, placements):
        early.append(round(a_start - place, 3))
    report["simulated_placement"] = {
        "intra_run_silence_s": round(intra_silence, 2),
        "intra_run_silence_pct_of_voice": round(intra_silence / max(0.001, voice_total) * 100, 1),
        "max_early_lead_s": max(early) if early else None,
        "anchors_started_early": sum(1 for e in early if e > 0.05),
        "track_end_s": round(track_end, 1),
    }

    # 5) real pauses in the produced voice track
    events = silence_events(DATA / "tts" / f"{job}.voice.m4a")
    long = [e for e in events if e["duration"] >= 0.3]
    report["voice_track"] = {
        "detected_pauses": len(events),
        "pause_seconds_total": round(sum(e["duration"] for e in events), 1),
        "pauses_ge_300ms": len(long),
        "longest_pause_s": round(max((e["duration"] for e in events), default=0), 2),
    }

    # 6) subtitle shape + glossary hygiene
    speech = list(csv.DictReader((DATA / "subtitles" / f"{job}.speech.tsv").open(), delimiter="\t"))
    per_anchor = {}
    for r in speech:
        per_anchor.setdefault(r["ANCHOR_ID"], []).append(r["TEXT"])
    mid = sum(1 for r in speech if r["TEXT"] and not r["TEXT"].rstrip().endswith((".", "!", "?", "…", ":", '"')))
    qa_path = DATA / "subtitles" / f"{job}.qa.json"
    overlong = json.loads(qa_path.read_text()).get("overlong_display_cues") if qa_path.exists() else None
    glossary_path = DATA / "subtitles" / f"{job}.glossary.json"
    variants = 0
    if glossary_path.exists():
        variants = sum(1 for e in json.loads(glossary_path.read_text()) if "/" in e.get("term_vi", ""))
    report["subtitle_shape"] = {
        "rows": len(speech),
        "rows_per_anchor": round(len(speech) / max(1, len(per_anchor)), 2),
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
