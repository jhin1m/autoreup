"""ASR coverage gap-fill (evidence pass).

Whisper large-v3-turbo collapses on music-heavy stretches (anime OP/insert songs)
inside a 60s chunk: it loops or returns nothing, and the pipeline's overlap dedupe
removes the repeated fragments — leaving multi-second ranges with real speech but
no rail segments, which then cannot be translated (measured on a real job: 3 gaps,
~100s, VAD speech 19-57% inside them; the same slice transcribed fine on its own).

This pass finds those gaps and re-runs the worker on them in small windows.
Segments recovered here are filtered harder than the main pass:
* no Han characters          -> whisper artifact ("... 数 heading"), dropped;
* repeated/looping text      -> song lyrics, dropped (the original music plays);
* otherwise                  -> merged into the rail.
"""

from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from urllib.request import Request, urlopen

from .models import AsrSegment
from .onset import _speech_probs


# Escalation ladder for a gap slice. The first attempt that yields usable rows wins;
# a music/ending-card slice often needs a forced language or the non-turbo model
# (measured: turbo returned "oh"/"不然,不然" loops on an ending card where forced-ja
# and non-turbo both read "ご視聴ありがとうございました").
_ATTEMPTS: list[dict] = [
    {"language": "auto"},
    {"language": "ja"},
    {"language": "zh"},
    {"language": "auto", "model": "@cf/openai/whisper"},  # non-turbo: slower, steadier
]


@dataclass(slots=True)
class CoverageConfig:
    enabled: bool = True
    min_gap: float = 5.0        # coarse fallback when VAD is unavailable
    min_hole_seconds: float = 0.8  # smallest VAD-speech hole worth a re-run
    max_holes: int = 60            # bound the number of worker calls per job
    speech_floor: float = 0.10  # share of VAD frames above threshold inside the gap
    slice_seconds: float = 25.0
    slice_overlap: float = 1.0


def find_gaps(spans: list[tuple[float, float]], duration: float, min_gap: float = 5.0) -> list[tuple[float, float]]:
    """Ranges of the video with no rail segment, longer than ``min_gap``."""
    gaps: list[tuple[float, float]] = []
    prev_end = 0.0
    for start, end in sorted(spans):
        if start - prev_end >= min_gap:
            gaps.append((prev_end, start))
        prev_end = max(prev_end, end)
    if duration - prev_end >= min_gap:
        gaps.append((prev_end, duration))
    return [(round(a, 3), round(b, 3)) for a, b in gaps]


def find_speech_holes(
    spans: list[tuple[float, float]],
    packed,
    min_seconds: float = 0.8,
    merge_gap: float = 0.4,
) -> list[tuple[float, float]]:
    """VAD-speech frames NOT covered by any span, grouped into ranges.

    The coarse ``find_gaps`` only sees holes >= min_gap; measured on a real job that
    left 3.2s of speech untexted in short holes (0.1-1.5s each, e.g. 48.3-49.8s
    inside a Japanese scene). This walks the VAD directly so any speech-bearing hole
    is found, with a floor and a merge distance to skip jitter/breath frames."""
    if packed is None:
        return []
    probs, step = packed
    raw: list[float] = []
    ordered = sorted(spans)
    for index, value in enumerate(probs):
        if value <= 0.5:
            continue
        t = index * step
        if any(start - 1e-6 <= t <= end + 1e-6 for start, end in ordered):
            continue
        raw.append(t)
    if not raw:
        return []
    ranges: list[tuple[float, float]] = []
    start = prev = raw[0]
    for t in raw[1:]:
        if t - prev > merge_gap:
            ranges.append((start, prev))
            start = t
        prev = t
    ranges.append((start, prev))
    return [(round(a, 3), round(b, 3)) for a, b in ranges if b - a >= min_seconds - 1e-9]


def speech_seconds(packed, start: float, end: float) -> float:
    """VAD speech seconds inside ``[start, end]`` (the source speech a row covers)."""
    if packed is None or end <= start:
        return 0.0
    probs, step = packed
    lo, hi = int(start / step), min(len(probs), int(end / step) + 1)
    if hi <= lo:
        return 0.0
    return sum(1 for value in probs[lo:hi] if value > 0.5) * step


def speech_fraction(packed, start: float, end: float) -> float:
    if packed is None:
        return 0.0
    probs, step = packed
    lo, hi = int(start / step), min(len(probs), int(end / step) + 1)
    if hi <= lo:
        return 0.0
    window = probs[lo:hi]
    if not len(window):
        return 0.0
    return sum(1 for value in window if value > 0.5) / len(window)


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def filter_recovered(rows: list[dict]) -> tuple[list[dict], int]:
    """Keep only Han-bearing, non-looping rows (see module docstring)."""
    kept: list[dict] = []
    skipped = 0
    for row in rows:
        text = str(row.get("text") or "").strip()
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in text)  # Han (zh or ja kanji)
        has_kana = any("\u3040" <= ch <= "\u30ff" for ch in text)  # hiragana/katakana
        if not text or not (has_cjk or has_kana):
            skipped += 1  # latin-only / empty: whisper artifact
            continue
        if kept and _similar(kept[-1]["text"], text) > 0.85 and len(text) >= 6:
            skipped += 1  # near-identical repeat: whisper loop
            continue
        kept.append({**row, "text": text})
    return kept, skipped


def fuse_rails(ocr: list[AsrSegment], asr: list[AsrSegment], overlap_ratio: float = 0.5) -> tuple[list[AsrSegment], dict]:
    """OCR (the burned-in script) wins where it has captions; ASR fills the rest.

    An ASR segment is dropped when most of its span already carries an OCR caption
    (the caption is the authored script — no homophone mistakes). ASR segments with
    no caption (original-language scenes, songs without subs) stay."""
    kept: list[AsrSegment] = []
    dropped = 0
    for segment in asr:
        duration = max(0.001, segment.end - segment.start)
        overlap = sum(max(0.0, min(segment.end, o.end) - max(segment.start, o.start)) for o in ocr)
        if overlap / duration > overlap_ratio:
            dropped += 1
            continue
        kept.append(segment)
    merged = sorted([*ocr, *kept], key=lambda seg: (seg.start, seg.end))
    merged = [AsrSegment(id=index, start=seg.start, end=seg.end, text_zh=seg.text_zh) for index, seg in enumerate(merged)]
    return merged, {"ocr": len(ocr), "asr_kept": len(kept), "asr_overlapped": dropped}


def dedupe_overlaps(segments: list[AsrSegment], min_keep: float = 0.2) -> list[AsrSegment]:
    """Enforce the rail invariant: sorted, non-overlapping segments.

    Nested spans (a gap-fill insert inside a long ASR blob) otherwise create nested
    anchors — the unit builder then shoves the later unit past the container's end and
    the same second of audio gets voiced and subtitled twice."""
    ordered = sorted(segments, key=lambda seg: (seg.start, seg.end))
    out: list[AsrSegment] = []
    for seg in ordered:
        if out and seg.start < out[-1].end - 0.05:
            prev = out[-1]
            if seg.end <= prev.end + 0.05:  # nested: keep the tighter time claim
                if (seg.end - seg.start) < (prev.end - prev.start):
                    out[-1] = seg
                continue
            prev.end = round(seg.start, 3)  # partial: trim the earlier tail
            if prev.end - prev.start < min_keep:
                out.pop()
                if out and out[-1].end > seg.start:
                    out[-1].end = round(seg.start, 3)
        out.append(seg)
    return [AsrSegment(id=index, start=seg.start, end=seg.end, text_zh=seg.text_zh) for index, seg in enumerate(out)]


def speech_coverage(spans: list[tuple[float, float]], packed, step: float | None = None) -> tuple[float, float, float]:
    """(speech_seconds_covered, speech_seconds_total, ratio) from the VAD probs."""
    if packed is None:
        return 0.0, 0.0, 0.0
    probs, size = packed
    total = float(sum(1 for value in probs if value > 0.5)) * size
    if total <= 0:
        return 0.0, 0.0, 0.0
    covered = 0.0
    for start, end in spans:
        lo, hi = int(start / size), min(len(probs), int(end / size) + 1)
        covered += sum(1 for value in probs[lo:hi] if value > 0.5) * size
    covered = min(covered, total)
    return covered, total, covered / total


def score_rows(rows: list[dict]) -> float:
    """Prefer longer CJK/kana content and punish repetition loops."""
    kept, skipped = filter_recovered(rows)
    chars = sum(len(row["text"]) for row in kept)
    return float(chars) - 30.0 * (skipped + len(rows) - len(kept))


def _transcribe_slice(
    worker_url: str,
    video: Path,
    start: float,
    length: float,
    language: str,
    model: str,
) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "slice.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(length), "-i", str(video),
             "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", "-f", "mp3", str(audio)],
            check=True,
        )
        body = json.dumps({
            "audio": base64.b64encode(audio.read_bytes()).decode(),
            "offset": start,
            "language": language,
            "model": model,
            # Whisper's own VAD strips sung/spoken-over-music stretches — the exact
            # content this pass exists to recover.
            "vad_filter": False,
        }).encode()
    request = Request(
        worker_url.rstrip("/") + "/transcribe",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "AutoReup-ASR/1.0"},
        method="POST",
    )
    with urlopen(request, timeout=180) as response:
        data = json.load(response)
    return data.get("segments") or data.get("result", {}).get("segments") or []


def fill_coverage_gaps(
    *,
    video: Path,
    segments: list[AsrSegment],
    duration: float,
    worker_url: str,
    model: str,
    language: str = "auto",
    packed=None,
    config: CoverageConfig | None = None,
) -> tuple[list[AsrSegment], dict]:
    config = config or CoverageConfig()
    stats = {"enabled": config.enabled, "gaps": 0, "filled": 0, "recovered": 0, "skipped": 0, "seconds": 0.0, "unrecovered": 0.0}
    if not config.enabled or not worker_url:
        return segments, stats

    spans = [(s.start, s.end) for s in segments]
    gaps = find_speech_holes(spans, packed, min_seconds=config.min_hole_seconds) if packed is not None else []
    if not gaps:
        gaps = find_gaps(spans, duration, config.min_gap)
    gaps = [gap for gap in gaps if gap[1] - gap[0] >= config.min_hole_seconds]
    if len(gaps) > config.max_holes:
        gaps = sorted(gaps, key=lambda gap: gap[1] - gap[0], reverse=True)[: config.max_holes]
    stats["gaps"] = len(gaps)
    if not gaps:
        return segments, stats
    if packed is None:
        packed = _speech_probs(video)

    recovered: list[AsrSegment] = []
    for gap_start, gap_end in gaps:
        if speech_fraction(packed, gap_start, gap_end) < config.speech_floor:
            continue  # genuinely silent/music-only: nothing to translate
        stats["filled"] += 1
        stats["seconds"] += round(gap_end - gap_start, 1)
        cursor = gap_start
        while cursor < gap_end - 1.0:
            length = min(config.slice_seconds, gap_end - cursor)
            best_rows: list[dict] = []
            best_score = -1e9
            for attempt in _ATTEMPTS:
                try:
                    rows = _transcribe_slice(
                        worker_url, video, cursor, length,
                        attempt.get("language", language), attempt.get("model", model),
                    )
                except Exception:  # noqa: BLE001 - coverage is best-effort; the main rail stands
                    break
                score = score_rows(rows)
                if score > best_score:
                    best_rows, best_score = rows, score
                if best_score >= 8.0:  # good enough: two-plus CJK characters, no loop
                    break
            kept, skipped = filter_recovered(best_rows)
            stats["skipped"] += skipped
            if not kept:
                stats["unrecovered"] += round(min(config.slice_seconds, gap_end - cursor), 1)
            slice_end = cursor + length
            for r in kept:
                # Whisper's segment ends can overshoot the slice; an unclamped span
                # injects dead air into the rail (and a bogus anchor window with it).
                start = max(cursor, min(float(r.get("start", cursor)), slice_end))
                end = min(slice_end, max(float(r.get("end", start + 1)), start))
                if end - start < 0.2:
                    continue
                recovered.append(AsrSegment(id=0, start=round(start, 3), end=round(end, 3), text_zh=str(r["text"])))
            cursor += max(1.0, length - config.slice_overlap)

    if recovered:
        # The ladder can hallucinate on music ("ご視聴ありがとうございました" is Whisper's
        # classic Japanese artifact): keep only what VAD agrees is speech.
        from .onset import tighten_to_speech

        recovered, hallucinated = tighten_to_speech(recovered, packed if packed is not None else _speech_probs(video))
        stats["skipped"] += hallucinated

    if not recovered:
        return segments, stats

    merged = sorted([*segments, *recovered], key=lambda s: (s.start, s.end))
    merged = [AsrSegment(id=index, start=s.start, end=s.end, text_zh=s.text_zh) for index, s in enumerate(merged)]
    stats["recovered"] = len(recovered)
    return merged, stats
