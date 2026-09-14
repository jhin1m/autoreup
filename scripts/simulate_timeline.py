#!/usr/bin/env python3
"""Simulate the v3 run-based timeline against the current per-anchor planner on
finished jobs — evidence only, never touches the pipeline.

Both planners are fed the SAME real data: anchors (as units) + measured TTS
durations. Reports, per job:
  * choppy gaps (>0.3s between voices inside one speech run): count + seconds
  * audible breaths (>0.15s): count
  * max early lead vs source
  * runs flagged for expand / overflow
  * extra silence pushed to run boundaries (voice ends early before a real pause)

Usage: .venv/bin/python scripts/simulate_timeline.py <job_id> [...]
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

from backend.app.dubbing.models import Unit  # noqa: E402
from backend.app.dubbing.timeline import TimelineConfig, plan_timeline  # noqa: E402

RUN_GAP = 0.8
OLD_GLUE_GAP = 0.30
OLD_MAX_LEAD = 0.3
CROSSFADE = 0.06
CHOPPY = 0.30
AUDIBLE = 0.15


def load(job: str) -> tuple[list[Unit], dict[int, float]]:
    rows = list(csv.DictReader((DATA / "subtitles" / f"{job}.anchors.tsv").open(), delimiter="\t"))
    units = [
        Unit(id=int(r["ID"]), segment_ids=r["SOURCE_IDS"], start=float(r["START"]), end=float(r["END"]), source_zh=r["SOURCE_ZH"])
        for r in rows
    ]
    timings = json.loads((DATA / "tts" / f"{job}.tts.json").read_text())
    durations = {int(t["anchor_id"]): float(t["tts_duration"]) for t in timings.get("timings", [])}
    return units, durations


def old_metrics(units: list[Unit], durations: dict[int, float]) -> dict:
    """Frozen copy of the legacy per-anchor planner (deleted from the app) — kept
    here so the simulation can compare old vs new placement on real data."""
    segments = [(u.start, Path("x"), durations.get(u.id, 0.0), u.end) for u in units if u.id in durations]
    placements = []
    cursor = 0.0
    prev_end: float | None = None
    for start, path, duration, end in segments:
        if prev_end is None:
            place, glued = start, False
        elif start - prev_end < OLD_GLUE_GAP:
            place, glued = max(0.0, cursor - CROSSFADE, start - OLD_MAX_LEAD), True
        else:
            place, glued = max(start, cursor), False
        placements.append((place, path, duration, glued))
        cursor = place + duration
        prev_end = end
    choppy_s = choppy_n = audible_n = 0.0
    for i in range(1, len(placements)):
        prev_place, _, prev_dur, _ = placements[i - 1]
        place, _, _, _ = placements[i]
        gap = place - (prev_place + prev_dur)
        inside = segments[i][0] - segments[i - 1][3] < RUN_GAP
        if inside and gap > CHOPPY:
            choppy_s += gap
            choppy_n += 1
        if inside and gap > AUDIBLE:
            audible_n += 1
    return {"choppy_gaps": choppy_n, "choppy_s": round(choppy_s, 1), "audible_gaps": audible_n}


def new_metrics(units: list[Unit], durations: dict[int, float], expand_est: bool = False) -> dict:
    cfg = TimelineConfig(run_gap=RUN_GAP)
    # Estimate the fit loop's effect: runs flagged for expand get their text
    # lengthened (assume up to +35% voice, capped at 90% fill), then re-plan.
    if expand_est:
        from backend.app.dubbing.timeline import build_runs, plan_run
        durations = dict(durations)
        for run in build_runs(units, cfg):
            plan = plan_run(run, units, durations, cfg)
            if plan.needs_expand:
                span = run.end - run.start
                voice = sum(durations.get(uid, 0.0) for uid in run.unit_ids)
                growth = min(1.35, max(1.05, span / max(0.001, voice)))
                for uid in run.unit_ids:
                    durations[uid] = durations.get(uid, 0.0) * growth
    plans = plan_timeline(units, durations, cfg)
    choppy_s = choppy_n = audible_n = 0.0
    max_lead = 0.0
    expand_runs = overflow_runs = 0
    for plan in plans:
        for placement in plan.placements[1:]:
            gap = placement.breath_before
            if gap > CHOPPY:
                choppy_s += gap
                choppy_n += 1
            if gap > AUDIBLE:
                audible_n += 1
        max_lead = max(max_lead, max((p.early_lead for p in plan.placements), default=0.0))
        expand_runs += 1 if plan.needs_expand else 0
        overflow_runs += 1 if plan.overflow_seconds > 0.05 else 0
    return {
        "choppy_gaps": int(choppy_n),
        "choppy_s": round(choppy_s, 1),
        "audible_gaps": int(audible_n),
        "max_early_lead_s": round(max_lead, 2),
        "expand_runs": expand_runs,
        "overflow_runs": overflow_runs,
    }


def main() -> None:
    jobs = sys.argv[1:]
    if not jobs:
        print(__doc__)
        sys.exit(1)
    results = []
    for job in jobs:
        units, durations = load(job)
        old = old_metrics(units, durations)
        new = new_metrics(units, durations)
        fitted = new_metrics(units, durations, expand_est=True)
        results.append({"job": job, "units": len(units), "old": old, "new": new, "after_expand": fitted})
        print(f"{job[:10]:<12} old choppy {old['choppy_gaps']:>3} gaps/{old['choppy_s']:>6.1f}s   ->   "
              f"new {new['choppy_gaps']} gaps | lead {new['max_early_lead_s']:>5}s | expand {new['expand_runs']}   ->   "
              f"after expand: lead {fitted['max_early_lead_s']:>4}s | expand {fitted['expand_runs']} | overflow {fitted['overflow_runs']}")
    (DATA / "audit").mkdir(exist_ok=True)
    (DATA / "audit" / "timeline_sim.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    old_total = sum(r["old"]["choppy_s"] for r in results)
    new_total = sum(r["new"]["choppy_s"] for r in results)
    print(f"\nTOTAL choppy silence: old {old_total:.1f}s -> new {new_total:.1f}s "
          f"({(1 - new_total / max(0.001, old_total)) * 100:.0f}% reduction)")


if __name__ == "__main__":
    main()
