"""Data-driven glossary extraction.

One LLM pass over the source transcript pulls out names/places/terms/numbers to
keep consistent across the whole video. No hardcoded per-genre tables: the model
infers the domain from the content and genre is only an optional hint.
"""

from __future__ import annotations

from backend.app.agents import AgentConfig, complete

from .models import Anchor, GlossaryEntry
from .prompts import GLOSSARY_SYSTEM, GLOSSARY_USER
from .text import clean
from .tsv_io import parse_tsv_rows

# Cap the transcript handed to a single extraction call (chars).
_MAX_SOURCE_CHARS = 12_000


def _source_text(anchors: list[Anchor]) -> str:
    joined = "\n".join(f"{a.id}\t{a.source_zh}" for a in anchors if a.source_zh.strip())
    return joined[:_MAX_SOURCE_CHARS]


def extract_glossary(anchors: list[Anchor], config: AgentConfig, genre: str = "", notes: str = "") -> list[GlossaryEntry]:
    source = _source_text(anchors)
    if not source.strip():
        return []
    user = GLOSSARY_USER.format(genre=genre or "(không rõ, tự suy ra)", notes=notes or "(không có)", source=source)
    raw = complete(GLOSSARY_SYSTEM, user, config)
    entries: list[GlossaryEntry] = []
    seen: set[str] = set()
    for cells in parse_tsv_rows(raw, expected_cols=4):
        term_zh, term_vi, kind, note = (clean(c) for c in cells)
        if not term_zh or not term_vi or term_zh in seen:
            continue
        term_vi = term_vi.split("/")[0].strip()
        if not term_vi:
            continue
        seen.add(term_zh)
        entries.append(GlossaryEntry(term_zh=term_zh, term_vi=term_vi, kind=kind.lower(), note=note))
    return entries


def format_glossary(entries: list[GlossaryEntry]) -> str:
    if not entries:
        return "(trống)"
    lines = []
    for entry in entries:
        suffix = f" — {entry.note}" if entry.note else ""
        kind = f" [{entry.kind}]" if entry.kind else ""
        lines.append(f"{entry.term_zh} => {entry.term_vi}{kind}{suffix}")
    return "\n".join(lines)
