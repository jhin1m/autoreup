"""TSV / JSON / SRT IO helpers.

The dubbing stages pass typed objects in-process; these helpers only persist
artifacts to disk (for inspection, resume, and the job manifest) and parse the
ASR SRT the worker produces.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from .text import clean


def read_tsv(path: Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=fields, delimiter="\t", extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_srt_time(value: str) -> float:
    hms, ms = value.split(",", 1)
    h, m, s = (int(part) for part in hms.split(":"))
    return h * 3600 + m * 60 + s + int(ms) / 1000


def format_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    millis = int(round(seconds * 1000))
    h, millis = divmod(millis, 3_600_000)
    m, millis = divmod(millis, 60_000)
    s, millis = divmod(millis, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"


def parse_srt(path: Path) -> list[dict[str, Any]]:
    """Parse an SRT file into ``{id, start, end, text}`` micro-segments."""
    text = Path(path).read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", text.strip())
    rows: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_raw, end_raw = (part.strip() for part in lines[1].split("-->", 1))
        rows.append(
            {
                "id": len(rows),
                "start": parse_srt_time(start_raw),
                "end": parse_srt_time(end_raw),
                "text": clean("".join(lines[2:])),
            }
        )
    return rows


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


def parse_tsv_rows(text: str, expected_cols: int) -> list[list[str]]:
    """Parse loose agent output into rows of exactly ``expected_cols`` columns.

    Strips code fences, ignores blank/markdown lines, and splits on tabs (falling
    back to runs of 2+ spaces). Rows with too few columns are skipped; extra
    columns are joined into the last field.
    """
    cleaned = _FENCE_RE.sub("", text.strip())
    rows: list[list[str]] = []
    for raw in cleaned.splitlines():
        line = raw.rstrip("\r\n")  # keep trailing tabs so empty final columns survive
        if not line.strip() or line.lstrip().startswith(("#", "//", "|---", "```")):
            continue
        cells = line.split("\t") if "\t" in line else re.split(r"\s{2,}", line.strip())
        cells = [cell.strip() for cell in cells]
        if len(cells) < expected_cols:
            continue
        if len(cells) > expected_cols:
            cells = cells[: expected_cols - 1] + ["\t".join(cells[expected_cols - 1 :])]
        rows.append(cells)
    return rows
