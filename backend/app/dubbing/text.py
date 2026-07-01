"""Text utilities shared across dubbing stages.

One source of truth for whitespace cleanup, Han detection, the explicit-turn
marker, and the splitters that turn a Vietnamese line into TTS/display cues.
"""

from __future__ import annotations

import re

# Detects any Han character. Vietnamese output must never contain one.
HAN_RE = re.compile(r"[一-鿿㐀-䶿]")

# In-band delimiter the translator/editor uses to mark an explicit speaker turn
# inside a single anchor. The splitter breaks on it before any length-based split.
EXPLICIT_TURN_MARKER = "⟂"  # ⟂
EXPLICIT_TURN_BREAK_RE = re.compile(r"\s*" + EXPLICIT_TURN_MARKER + r"\s*")

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+")
_MIN_COMMA_SPLIT_LEN = 18


def clean(text: object) -> str:
    """Collapse whitespace/newlines into single spaces and strip ends."""
    return " ".join(str(text or "").replace("\n", " ").split()).strip()


def has_han(text: object) -> bool:
    return bool(HAN_RE.search(str(text or "")))


# --- caption sanitation ----------------------------------------------------
# The translator is fed rows shaped ANCHOR_ID<TAB>TIME<TAB>≤CHARS<TAB>SOURCE and
# sometimes echoes the TIME/≤CHARS columns into its TEXT, or emits a stray '|' / '/'
# where it should use ⟂, or doubles a comma. None of that should reach the voice or
# the subtitle, so every translated line is run through sanitize_caption.
_LEAD_TIME_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?\s*(?:≤\s*\d+)?\s*")
_LEAD_CPS_RE = re.compile(r"^\s*≤\s*\d+\s*")
_LEAD_JUNK_RE = re.compile(r"^[\s|/·•▪◦>»]+")
_SEP_RE = re.compile(r"\s[|/]\s")                     # spaced pipe/slash used as a separator
_MULTI_COMMA_RE = re.compile(r"\s*[,，](?:\s*[,，])+")  # ",," / ", ," / "，，" -> ", "
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?…])")
_EMPTY_QUOTES_RE = re.compile(r"[\"“”]{2,}")           # "", “”, """ -> gone
_TRAIL_JUNK_RE = re.compile(r"[\s,;:|/·–—]+$")         # trailing bare separators (keep . ! ? … and quotes)


def sanitize_caption(text: object) -> str:
    """Strip leaked input columns / stray separators / doubled punctuation the LLM adds,
    so neither the TTS nor the subtitle carries junk like '8.03-14.39 ≤114 ...', '| ...',
    '..., / ...', ',,' or '""'."""
    t = clean(text)
    t = _LEAD_TIME_RE.sub("", t)
    t = _LEAD_CPS_RE.sub("", t)
    t = _LEAD_JUNK_RE.sub("", t)
    t = _SEP_RE.sub(" ", t)
    t = t.replace("|", " ")           # any remaining stray pipe (never valid in captions)
    t = _EMPTY_QUOTES_RE.sub("", t)
    t = _MULTI_COMMA_RE.sub(", ", t)
    t = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", t)
    t = _TRAIL_JUNK_RE.sub("", t)
    return clean(t)


def strip_explicit_turn_markers(text: object) -> str:
    return clean(EXPLICIT_TURN_BREAK_RE.sub(" ", clean(text)))


def split_text_for_tts(text: str, max_chars: int) -> list[str]:
    """Split a line into cue-sized parts on sentence then comma boundaries."""
    text = clean(text)
    if not text:
        return []

    parts: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        parts.append(text[start:match.start()].strip())
        start = match.end()
    tail = text[start:].strip()
    if tail:
        parts.append(tail)

    out: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            out.append(part)
            continue
        remaining = part
        while len(remaining) > max_chars and "," in remaining:
            commas = [m.start() for m in re.finditer(",", remaining)]
            midpoint = len(remaining) / 2
            pos = min(commas, key=lambda item: abs(item - midpoint))
            left = remaining[: pos + 1].strip()
            right = remaining[pos + 1 :].strip()
            if len(left) < _MIN_COMMA_SPLIT_LEN or len(right) < _MIN_COMMA_SPLIT_LEN:
                break
            out.append(left)
            remaining = right
        if remaining:
            out.append(remaining)
    return out


def split_text_for_explicit_turns(text: str, max_chars: int) -> list[str]:
    """Break on explicit-turn markers first, then length-split each group."""
    text = clean(text)
    if not text:
        return []
    groups = [part for part in (clean(part) for part in EXPLICIT_TURN_BREAK_RE.split(text)) if part]
    if not groups:
        groups = [text]
    parts: list[str] = []
    for group in groups:
        if len(group) <= max_chars:
            parts.append(group)
        else:
            parts.extend(split_text_for_tts(group, max_chars))
    return [part for part in parts if part]
