"""Voice render + measure.

Builds one TTS block per anchor (so overflow is measured per anchor), synthesizes
each via the TTS provider with a disk cache, tempo-fits it into the anchor window,
assembles an absolutely-scheduled voice track, and reports per-anchor timing. An
anchor whose fitted speech still overruns its window by more than the threshold is
flagged ``needs_repair`` for the repair loop — overflow never pushes later anchors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from backend.app.services.audio import align_audio, assemble_track, probe_duration, require_ffmpeg
from backend.app.services.tts import (
    HttpTtsProvider,
    TtsBlock,
    clean_tts_text,
    legacy_raw_audio_path,
    raw_audio_path,
)
from .models import Anchor, SpeechRow
from .text import strip_explicit_turn_markers


@dataclass(slots=True)
class RenderConfig:
    provider_url: str
    speaker: str = "BV421_vivn_streaming"
    speed: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0
    text_mode: str = "continuous"
    min_tempo: float = 1.0
    max_tempo: float = 1.18
    timeout: float = 30.0
    attempts: int = 3
    repair_over_ms: int = 300  # overflow above this flags an anchor for repair
    voice_lead: float = 0.0    # extra slack added to each anchor window


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

    def anchors_need_repair(self) -> list[int]:
        return [t.anchor_id for t in self.timings if t.needs_repair]


def _blocks_by_anchor(speech_rows: list[SpeechRow], anchors: list[Anchor], text_mode: str) -> list[TtsBlock]:
    by_anchor: dict[int, list[SpeechRow]] = {}
    for row in speech_rows:
        by_anchor.setdefault(row.anchor_id, []).append(row)
    blocks: list[TtsBlock] = []
    for anchor in anchors:
        rows = by_anchor.get(anchor.id)
        if not rows:
            continue
        joined = " ".join(strip_explicit_turn_markers(r.text) for r in rows)
        text = clean_tts_text(joined, text_mode=text_mode)
        if not text:
            continue
        blocks.append(
            TtsBlock(
                block_id=str(anchor.id),
                start=round(anchor.start, 3),
                end=round(anchor.end, 3),
                text=text,
                source_cue_ids=tuple(r.sub_id for r in rows),
            )
        )
    return blocks


def render(
    speech_rows: list[SpeechRow],
    anchors: list[Anchor],
    config: RenderConfig,
    work_dir: Path,
    cache_dir: Path,
    output_audio: Path,
) -> RenderResult:
    require_ffmpeg()
    blocks = _blocks_by_anchor(speech_rows, anchors, config.text_mode)
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
    segments: list[tuple[float, Path]] = []
    timings: list[AnchorTiming] = []

    for index, block in enumerate(blocks):
        raw_path = raw_audio_path(
            cache_dir, config.provider_url, config.speaker, block,
            speed=config.speed, pitch=config.pitch, volume=config.volume, text_mode=config.text_mode,
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not (raw_path.exists() and raw_path.stat().st_size > 1024):
            legacy = legacy_raw_audio_path(
                cache_dir, config.provider_url, config.speaker, block,
                speed=config.speed, pitch=config.pitch, volume=config.volume, text_mode=config.text_mode,
            )
            if legacy.exists() and legacy.stat().st_size > 1024:
                raw_path.write_bytes(legacy.read_bytes())
            else:
                raw_path.write_bytes(provider.synthesize(block.text))

        target = block.duration + config.voice_lead
        aligned = work_dir / "aligned" / f"block-{index:05d}.m4a"
        timing = align_audio(raw_path, aligned, target, min_tempo=config.min_tempo, max_tempo=config.max_tempo)
        segments.append((block.start, aligned))
        overflow_ms = int(timing["overflow_ms"])
        timings.append(
            AnchorTiming(
                anchor_id=int(block.block_id),
                anchor_start=block.start,
                anchor_end=block.end,
                target_duration=round(target, 3),
                tts_duration=float(timing["playback_duration"]),
                overflow_ms=overflow_ms,
                needs_repair=overflow_ms > config.repair_over_ms,
            )
        )

    total_duration = max((b.end for b in blocks), default=0.1)
    output_audio = Path(output_audio)
    assemble_track(segments, output_audio, work_dir=work_dir, total_duration=total_duration)

    need_repair = [t for t in timings if t.needs_repair]
    summary = {
        "blocks": len(blocks),
        "anchors_need_repair": len(need_repair),
        "max_overflow_ms": max((t.overflow_ms for t in timings), default=0),
        "overflow_pushes_next_voice": False,  # absolute scheduling guarantees this
        "voice_track": str(output_audio),
        "total_duration": round(total_duration, 3),
    }
    return RenderResult(voice_track=output_audio, timings=timings, summary=summary)
