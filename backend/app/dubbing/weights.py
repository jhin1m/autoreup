"""Vietnamese speech-weight heuristic.

``vi_speech_weight`` estimates how long a Vietnamese line takes to speak, in
"syllable-equivalents", so the timing/repair stages can budget anchors without
synthesizing audio. Numbers are expanded to spoken syllables; punctuation adds
pause weight. This is the budget kernel reused by translate/edit/repair.
"""

from __future__ import annotations

import re

from .text import clean

VI_TOKEN_RE = re.compile(
    r"(?i)v\.v\.?|"
    r"\d+(?:[.,]\d+)*(?:[%a-zA-ZÀ-ỹĐđ]+)?|"
    r"[A-Za-zÀ-ỹĐđ]+(?:[-'][A-Za-zÀ-ỹĐđ]+)*"
)


def _two_digit_weight(value: int) -> float:
    value = max(0, min(value, 99))
    if value < 10:
        return 1.0 if value else 0.0
    ones = value % 10
    return 2.0 + (1.0 if ones else 0.0)


def _three_digit_weight(value: int) -> float:
    value = max(0, min(value, 999))
    hundreds = value // 100
    rest = value % 100
    if hundreds and rest:
        return 2.0 + _two_digit_weight(rest)
    if hundreds:
        return 2.0
    return _two_digit_weight(rest)


def integer_vi_weight(text: str) -> float:
    digits = text.lstrip("0") or "0"
    groups: list[int] = []
    while digits:
        groups.insert(0, int(digits[-3:]))
        digits = digits[:-3]
    total = 0.0
    for index, group in enumerate(groups):
        if group == 0:
            continue
        scale_index = len(groups) - index - 1
        total += _three_digit_weight(group)
        if scale_index:
            total += 1.0  # nghìn/triệu/tỷ... marker
    return max(1.0, total)


def _token_weight(token: str) -> float:
    token = token.strip()
    if not token:
        return 0.0
    if re.fullmatch(r"(?i)v\.v\.?", token):
        return 2.0  # vân vân
    if "-" in token and not re.match(r"^\d", token):
        return sum(_token_weight(part) for part in token.split("-") if part)

    number = re.match(r"^(\d+(?:[.,]\d+)*)(.*)$", token)
    if number:
        number_text, suffix = number.groups()
        if re.search(r"[.,]", number_text):
            parts = re.split(r"[.,]", number_text)
            weight = integer_vi_weight(parts[0]) + 1.0  # phẩy/chấm
            weight += sum(max(1, len(part)) for part in parts[1:] if part)
        else:
            weight = integer_vi_weight(number_text)
        if suffix:
            weight += 1.0
        return weight

    letters = re.sub(r"[^A-Za-zÀ-ỹĐđ]", "", token)
    if len(letters) >= 2 and letters.upper() == letters:
        return float(len(letters))
    return 1.0


def _punctuation_pause_weight(text: str) -> float:
    protected = re.sub(r"(?i)v\.v\.??", "", text)
    protected = re.sub(r"\d+[.,]\d+", "0", protected)
    ellipsis = protected.count("…") + protected.count("...")
    protected = protected.replace("...", "").replace("…", "")
    return (
        0.35 * len(re.findall(r"[,，;；:：]", protected))
        + 0.55 * len(re.findall(r"[.!?。！？]", protected))
        + 0.75 * ellipsis
        + 0.25 * len(re.findall(r"[—–]", protected))
    )


def vi_speech_weight(text: object) -> float:
    value = clean(text)
    if not value:
        return 1.0
    token_weight = sum(_token_weight(match.group(0)) for match in VI_TOKEN_RE.finditer(value))
    return max(1.0, token_weight + _punctuation_pause_weight(value))
