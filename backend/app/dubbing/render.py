"""Voice render + measure.

Builds one TTS block per anchor (so overflow is measured per anchor), synthesizes
each via the TTS provider with a disk cache, tempo-fits it into the anchor window,
assembles an absolutely-scheduled voice track, and reports per-anchor timing. An
anchor whose fitted speech still overruns its window by more than the threshold is
flagged ``needs_repair`` for the repair loop — overflow never pushes later anchors.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.services.audio import align_audio, assemble_track, master_voice, probe_duration, require_ffmpeg, trim_silence
from backend.app.services.tts import (
    HttpTtsProvider,
    TtsBlock,
    clean_tts_text,
    raw_audio_path,
)
from .display import build_timed_units
from .models import Anchor, AsrSegment, SpeechRow
from .text import strip_explicit_turn_markers


@dataclass(slots=True)
class RenderConfig:
    provider_url: str
    speaker: str = "BV421_vivn_streaming"
    speed: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0
    text_mode: str = "natural"  # keep punctuation -> natural pauses in the voice-over
    # Tempo stability: never slow below 1.0 (no dragging) — slowing short lines to
    # fill made the rate lurch between phrases. Only speed up long lines, up to
    # max; the repair loop shortens anything past that. Set fixed_tempo to force a
    # single rate for every clip (rigidly stable, but costs more overflow/repair).
    fixed_tempo: float | None = None
    min_tempo: float = 1.0
    max_tempo: float = 1.18
    timeout: float = 30.0
    attempts: int = 3
    repair_over_ms: int = 300  # overflow above this flags an anchor for repair
    voice_lead: float = 0.0    # extra slack added to each anchor window
    workers: int = 8           # parallel TTS synth + tempo-fit (calls are independent + cached)
    min_duration: float = 0.4  # sentence-unit floor (shared with the subtitle)
    # Smooth assembly: a source gap below glue_gap is NOT a real pause — the speaker
    # is talking continuously on screen — so those lines are butt-joined with a short
    # crossfade and the artificial underfill silence is dropped (no stop-start
    # "khựng"). A gap >= glue_gap is a real beat; its onset stays locked to the
    # picture ("khớp ảnh"). crossfade is the equal-power overlap at glued joins.
    glue_gap: float = 0.30
    crossfade: float = 0.06


@dataclass(slots=True)
class AnchorTiming:
    anchor_id: int
    anchor_start: float
    anchor_end: float
    target_duration: float
    tts_duration: float
    overflow_ms: int
    needs_repair: bool


@dataclass(slots=True)
class RenderResult:
    voice_track: Path | None
    timings: list[AnchorTiming] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    # (anchor_start, clip_path, playback_duration, anchor_end)
    segments: list[tuple[float, Path, float, float]] = field(default_factory=list)
    total_duration: float = 0.0

    def anchors_need_repair(self) -> list[int]:
        return [t.anchor_id for t in self.timings if t.needs_repair]


def assemble_voice(result: RenderResult, output_audio: Path, work_dir: Path, glue_gap: float = 0.30, crossfade: float = 0.06) -> Path:
    """Assemble the tempo-fitted clips smoothly, then master the voice track.

    Kept separate from :func:`render` so the repair loop can re-measure each round
    and assemble only once, after timings converge — not on every round. Smooth
    assembly butt-joins contiguous lines (crossfade, no artificial silence) and
    keeps absolute onsets only at real pauses; the mastering chain (EQ/compress/
    limit) is what makes the dub easy to listen to."""
    output_audio = Path(output_audio)
    work_dir = Path(work_dir)
    unmastered = work_dir / "voice.unmastered.m4a"
    assemble_track(result.segments, unmastered, work_dir=work_dir, total_duration=result.total_duration, glue_gap=glue_gap, crossfade=crossfade)
    master_voice(unmastered, output_audio)
    result.voice_track = output_audio
    result.summary["voice_track"] = str(output_audio)
    return output_audio


def _blocks_from_units(units: list[dict], text_mode: str) -> list[TtsBlock]:
    """One TTS block per SENTENCE unit — synthesized as a single continuous clip and
    placed at the sentence's source-segment time. A sentence is never split across
    clips, so a continuous line is never spoken in two pieces. ``block_id`` keeps the
    anchor id so timings aggregate per anchor for the repair loop."""
    blocks: list[TtsBlock] = []
    for unit in units:
        text = clean_tts_text(strip_explicit_turn_markers(unit["text"]), text_mode=text_mode)
        if not text:
            continue
        blocks.append(
            TtsBlock(
                block_id=str(unit["anchor_id"]),
                start=round(float(unit["start"]), 3),
                end=round(float(unit["end"]), 3),
                text=text,
            )
        )
    return blocks


def render(
    speech_rows: list[SpeechRow],
    anchors: list[Anchor],
    config: RenderConfig,
    work_dir: Path,
    cache_dir: Path,
    segments: list[AsrSegment] | None = None,
) -> RenderResult:
    """Synthesize + tempo-fit each PIECE at its source-segment time (in parallel) and
    report timing aggregated per anchor (so the repair loop still condenses by anchor).

    Does NOT assemble the final track — call :func:`assemble_voice` once after the
    repair loop converges, so intermediate rounds don't pay for assembly.
    """
    require_ffmpeg()
    units = build_timed_units(speech_rows, config.min_duration, anchors, segments)
    blocks = _blocks_from_units(units, config.text_mode)
    if not blocks:
        return RenderResult(voice_track=None, summary={"blocks": 0, "anchors_need_repair": 0})

    provider = HttpTtsProvider(
        config.provider_url,
        speaker=config.speaker,
        speed=config.speed,
        pitch=config.pitch,
        volume=config.volume,
        timeout=config.timeout,
        attempts=config.attempts,
    )
    cache_dir = Path(cache_dir)
    work_dir = Path(work_dir)

    def render_block(index: int, block: TtsBlock) -> tuple[tuple[float, Path, float, float], tuple[int, float]]:
        raw_path = raw_audio_path(
            cache_dir, config.provider_url, config.speaker, block,
            speed=config.speed, pitch=config.pitch, volume=config.volume, text_mode=config.text_mode,
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not (raw_path.exists() and raw_path.stat().st_size > 1024):
            raw_path.write_bytes(provider.synthesize(block.text))
            trim_silence(raw_path)  # drop the TTS head/tail silence so speech lands at placement

        target = block.duration + config.voice_lead
        # Cache the tempo-fitted clip too, keyed by (raw clip, target window, tempo
        # params). A repair round only changes a few sentences; every UNCHANGED block
        # then hits this cache and skips the expensive ffmpeg align entirely — the
        # repair loop stops re-rendering the whole video each round.
        aligned_key = hashlib.sha256(
            f"{raw_path.stem}|{round(target, 3)}|{config.min_tempo}|{config.max_tempo}|{config.fixed_tempo}".encode()
        ).hexdigest()[:24]
        aligned = cache_dir / "aligned" / f"{aligned_key}.m4a"
        if aligned.exists() and aligned.stat().st_size > 1024:
            playback = probe_duration(aligned)
        else:
            timing = align_audio(
                raw_path, aligned, target, min_tempo=config.min_tempo, max_tempo=config.max_tempo, fixed_tempo=config.fixed_tempo
            )
            playback = float(timing["playback_duration"])
        return (block.start, aligned, playback, block.end), (int(block.block_id), playback)

    # TTS synth + tempo-fit are independent per piece (and disk-cached), so run them
    # in parallel; results are reassembled in piece order.
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        rendered = list(pool.map(lambda pair: render_block(*pair), enumerate(blocks)))
    segments_out = [seg for seg, _ in rendered]

    # Aggregate piece playbacks per anchor and compare to the anchor's source window:
    # if the anchor's total speech overruns its window, the repair loop condenses it.
    anchor_window = {a.id: (a.start, a.end) for a in anchors}
    played: dict[int, float] = {}
    for _seg, (anchor_id, playback) in rendered:
        played[anchor_id] = played.get(anchor_id, 0.0) + playback

    timings: list[AnchorTiming] = []
    for anchor_id, total_play in played.items():
        a0, a1 = anchor_window.get(anchor_id, (0.0, total_play))
        target = max(0.1, a1 - a0)
        overflow_ms = int(round(max(0.0, total_play - target) * 1000))
        timings.append(AnchorTiming(
            anchor_id=anchor_id,
            anchor_start=a0,
            anchor_end=a1,
            target_duration=round(target, 3),
            tts_duration=round(total_play, 3),
            overflow_ms=overflow_ms,
            needs_repair=overflow_ms > config.repair_over_ms,
        ))

    total_duration = max((b.end for b in blocks), default=0.1)
    need_repair = [t for t in timings if t.needs_repair]
    summary = {
        "blocks": len(blocks),
        "anchors": len(timings),
        "anchors_need_repair": len(need_repair),
        "max_overflow_ms": max((t.overflow_ms for t in timings), default=0),
        "overflow_pushes_next_voice": False,  # absolute scheduling guarantees this
        "voice_track": None,
        "total_duration": round(total_duration, 3),
    }
    return RenderResult(voice_track=None, timings=timings, summary=summary, segments=segments_out, total_duration=total_duration)
