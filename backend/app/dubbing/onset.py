"""Align the ASR rail to real speech using Silero VAD (the published fix).

Whisper timestamps on noisy content (music beds, SFX) LEAD the actual words —
measured on production episodes: starts up to >1s early — and Whisper also
hallucinates text inside music-only passages. Everything downstream (anchors,
voice placement, subtitles) trusts the rail, so the dub spoke before the
on-screen character and "extra" lines appeared out of nowhere.

This is the standard remedy used by WhisperX / faster-whisper: run Silero VAD
(ONNX, no torch import) over the source audio, then

* snap each segment START forward to the first VAD speech region near it
  (starts only ever move forward; ends are left alone);
* drop segments that overlap NO speech at all (hallucinations).
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import wave
from pathlib import Path

from .models import AsrSegment

_SR = 16000
_WINDOW = 512            # samples per VAD step (32ms @ 16k) — silero v5 contract
_THRESHOLD = 0.5         # speech probability hysteresis: on above this…
_NEG_THRESHOLD = 0.35    # …off below this
_MIN_SPEECH = 0.25       # discard speech blips shorter than this (s)
_MIN_SILENCE = 0.25      # close speech gaps shorter than this (s)
_PAD = 0.05              # widen each speech region by this on both sides (s)
_MAX_SNAP = 1.5          # never move a start further than this (s)
_MIN_SNAP = 0.08         # ignore sub-frame corrections (s)
_KEEP_TAIL = 0.25        # never snap past end - this (keep room for the words)


def _model_path() -> Path | None:
    spec = importlib.util.find_spec("silero_vad")
    if spec is None or not spec.origin:
        return None
    path = Path(spec.origin).parent / "data" / "silero_vad.onnx"
    return path if path.exists() else None


def _read_audio(video: Path) -> "np.ndarray | None":  # noqa: F821 - numpy imported lazily
    import numpy as np

    with tempfile.TemporaryDirectory() as tmp:
        wav_path = Path(tmp) / "audio.wav"
        completed = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-ac", "1", "-ar", str(_SR), str(wav_path)],
            capture_output=True,
        )
        if completed.returncode != 0 or not wav_path.exists():
            return None
        with wave.open(str(wav_path)) as w:
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def _speech_probs(video: Path) -> "tuple[np.ndarray, float] | None":  # noqa: F821
    """Per-window Silero speech probability, ``(probs, window_seconds)``."""
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError:
        return None
    model = _model_path()
    if model is None:
        return None
    audio = _read_audio(video)
    if audio is None or not len(audio):
        return None

    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(_SR, dtype=np.int64)
    # Silero v5 contract: every 512-sample window is fed WITH the last 64 samples of
    # the previous window prepended (576 total). Without that context the model's
    # probabilities are garbage (~0 even on clear speech).
    context = np.zeros((1, 64), dtype=np.float32)
    total = len(audio) // _WINDOW
    probs = np.empty(total, dtype=np.float32)
    for i in range(total):
        chunk = audio[i * _WINDOW : (i + 1) * _WINDOW][None, :]
        out, state = session.run(None, {"input": np.concatenate([context, chunk], axis=1), "state": state, "sr": sr})
        probs[i] = out[0, 0]
        context = chunk[:, -64:]
    return probs, _WINDOW / _SR


def _regions_from_probs(probs, step: float) -> list[tuple[float, float]]:
    """Hysteresis-threshold the probability curve into merged speech spans."""
    total = len(probs)
    regions: list[tuple[float, float]] = []
    speaking = False
    start = 0.0
    for i, p in enumerate(probs):
        if not speaking and p >= _THRESHOLD:
            speaking, start = True, i * step
        elif speaking and p < _NEG_THRESHOLD:
            speaking = False
            regions.append((start, i * step))
    if speaking:
        regions.append((start, total * step))

    merged: list[tuple[float, float]] = []
    for s, e in regions:
        if merged and s - merged[-1][1] < _MIN_SILENCE:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return [
        (max(0.0, s - _PAD), e + _PAD)
        for s, e in merged
        if e - s >= _MIN_SPEECH
    ]


_DROP_MAX_PROB = 0.15  # music-only hallucinations sit <0.1; soft real speech ~0.2-0.4


def refine_segments(segments: list[AsrSegment], video: Path) -> tuple[list[AsrSegment], int, int]:
    """Snap starts to VAD speech onsets and drop hallucinated segments.

    Dropping is deliberately conservative — only when the VAD saw essentially NO
    speech-like signal anywhere in the segment (losing a real whispered line would
    be worse than keeping a phantom one). Returns ``(segments, moved, dropped)``.
    Best-effort: without VAD (missing dependency / decode failure) the rail is
    returned untouched."""
    packed = _speech_probs(video)
    if packed is None:
        return segments, 0, 0
    probs, step = packed
    regions = _regions_from_probs(probs, step)
    if not regions:
        return segments, 0, 0

    def max_prob(seg: AsrSegment) -> float:
        lo, hi = int(seg.start / step), min(len(probs), int(seg.end / step) + 1)
        return float(probs[lo:hi].max()) if hi > lo else 0.0

    kept: list[AsrSegment] = []
    moved = dropped = 0
    prev_end = 0.0
    for seg in segments:
        if max_prob(seg) < _DROP_MAX_PROB:
            dropped += 1  # nothing speech-like anywhere inside -> Whisper hallucination
            continue
        onset = next((s for s, e in regions if e > seg.start and s < seg.end), None)
        if onset is not None:
            snapped = min(max(onset, prev_end), seg.start + _MAX_SNAP, seg.end - _KEEP_TAIL)
            if snapped > seg.start + _MIN_SNAP:
                seg.start = round(snapped, 3)
                moved += 1
        kept.append(seg)
        prev_end = seg.end
    for index, seg in enumerate(kept):
        seg.id = index
    return kept, moved, dropped
