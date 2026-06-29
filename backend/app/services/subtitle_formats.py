import json
from pathlib import Path


def cue_text(cue: dict) -> str:
    return (cue.get("translated") or cue.get("vi_display") or cue.get("text") or "").strip()


def srt_time(second: float) -> str:
    ms = int(round(max(0.0, second) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def vtt_time(second: float) -> str:
    return srt_time(second).replace(",", ".")


def load_cues(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_srt(cues: list[dict], output: Path) -> None:
    lines = []
    for index, cue in enumerate(cues, 1):
        lines.extend([str(index), f"{srt_time(float(cue['start']))} --> {srt_time(float(cue['end']))}", cue_text(cue), ""])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")


def write_vtt(cues: list[dict], output: Path) -> None:
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.extend([f"{vtt_time(float(cue['start']))} --> {vtt_time(float(cue['end']))}", cue_text(cue), ""])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
