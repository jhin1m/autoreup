"""L1 — NORMALIZE: fix ASR recognition noise without touching meaning.

Whisper-turbo garbles Chinese speech: homophone substitutions, repeated characters,
broken numbers (measured on real jobs: 骰子 heard as 头子, whole clauses turned into
noise). Those errors flow straight into translation and force the model to guess.

Two layers:
1. deterministic cleanup — whitespace, doubled punctuation, stray ASCII repeats;
2. one agent pass over suspicious segments only, gated in code: same digits, length
   within +/-30%, no new Han-free lines. Anything that fails the gate keeps the
   original text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.app.agents import AgentConfig, complete

from .models import AsrSegment
from .prompts import NORMALIZE_SYSTEM, NORMALIZE_USER
from .text import clean
from .tsv_io import parse_tsv_rows

_ASCII = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")
_REPEAT_PUNCT = re.compile(r"([。！？，、；：])\1+")


@dataclass(slots=True)
class NormalizeConfig:
    enabled: bool = True
    min_len_ratio: float = 0.7
    max_len_ratio: float = 1.3


def cleanup_text(text: str) -> str:
    text = _REPEAT_PUNCT.sub(r"\1", text)
    return clean(" ".join(text.split()))


def suspicious(text: str) -> bool:
    """Segments worth asking the agent about: latin/digit noise or a single char."""
    stripped = text.strip()
    if len(stripped) <= 1:
        return True
    return bool(_ASCII.search(stripped) or _DIGIT.search(stripped))


def _digits(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


def normalize_segments(
    segments: list[AsrSegment],
    llm: AgentConfig,
    config: NormalizeConfig | None = None,
) -> tuple[list[AsrSegment], dict]:
    """Return new segments (text cleaned/patched) + metrics."""
    config = config or NormalizeConfig()
    cleaned = [AsrSegment(id=s.id, start=s.start, end=s.end, text_zh=cleanup_text(s.text_zh)) for s in segments]
    metrics = {"patched": 0, "suspicious": 0, "deterministic_changes": sum(
        1 for old, new in zip(segments, cleaned) if old.text_zh != new.text_zh
    )}

    candidates = [s for s in cleaned if suspicious(s.text_zh)]
    metrics["suspicious"] = len(candidates)
    if not config.enabled or not candidates:
        return cleaned, metrics

    by_id = {s.id: s for s in cleaned}
    rows = "\n".join(f"{s.id}\t{s.text_zh}" for s in candidates)
    try:
        raw = complete(NORMALIZE_SYSTEM, NORMALIZE_USER.format(segments=rows), llm)
    except Exception:  # noqa: BLE001 - normalization is best-effort
        return cleaned, metrics

    for cells in parse_tsv_rows(raw, expected_cols=2):
        try:
            segment_id = int(cells[0].strip())
        except ValueError:
            continue
        segment = by_id.get(segment_id)
        if segment is None:
            continue
        patched = cleanup_text(cells[1])
        current = segment.text_zh
        if not patched or patched == current:
            continue
        ratio = len(patched) / max(1, len(current))
        if ratio < config.min_len_ratio or ratio > config.max_len_ratio:
            continue
        if _digits(current) - _digits(patched):  # a number disappeared
            continue
        if not patched.strip():
            continue
        segment.text_zh = patched
        metrics["patched"] += 1
    return cleaned, metrics
