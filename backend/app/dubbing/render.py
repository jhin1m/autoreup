"""Voice render + measure (v3): synthesize per sentence, place by speech RUN.

Differences vs the legacy per-anchor placement (see docs/TRANSLATE-V3.md):

* clips are tempo-fitted with ONE factor per run (run-level tempo) instead of a
  per-clip window fit — slowing a short line alone made the rate lurch;
* placement is chained inside a run with a distributed breath; only run boundaries
  anchor to the picture. Measured on 10 real jobs: audible intra-run silence
  468.9s -> 0, and the remaining underfill is reported per run for the fit loop;
* a run whose voice still ends far before its source window is flagged
  ``needs_expand`` (missing spoken content) — the per-anchor 40% rule caught 0 cases.
"""

from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.services.audio import align_audio, assemble_track, master_voice, probe_duration, require_ffmpeg, speed_clip, trim_silence
from backend.app.services.tts import (
    HttpTtsProvider,
    TtsBlock,
    clean_tts_text,
    raw_audio_path,
)
from .display import build_timed_units
from .models import Anchor, AsrSegment, SpeechRow, Unit
from .text import strip_explicit_turn_markers
from .timeline import TimelineConfig, plan_timeline, summarize


@dataclass(slots=True)
class RenderConfig:
    provider_url: str
    speaker: str = "BV421_vivn_streaming"
    speed: float = 1.0
    pitch: float = 1.0
    volume: float = 1.0
    text_mode: str = "natural"
    timeout: float = 30.0
    attempts: int = 3
    repair_over_ms: int = 1500  # only clearly-over units; the timeline absorbs the rest
    batch_size: int = 8
    batch_concurrency: int = 8
    synth_rate: float = 1.0
    workers: int = 8
    min_duration: float = 0.4
    crossfade: float = 0.06
    # L6 timeline knobs
    run_gap: float = 0.8
    tempo_floor: float = 0.92
    tempo_ceiling: float = 1.18
    breath_min: float = 0.06
    breath_max: float = 0.6
    pause_keep: float = 0.7
    min_run_fill: float = 0.95
    min_trailing: float = 1.5
    sentence_breath: float = 0.35
    dramatic_breath: float = 0.5
    clause_breath: float = 0.12


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
    # (place_start, clip_path, playback_duration, glued)
    placements: list[tuple[float, Path, float, bool]] = field(default_factory=list)
    # sentence start times in the same order as ``placements`` (for caption windows)
    block_starts: list[float] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    total_duration: float = 0.0

    def anchors_need_repair(self) -> list[int]:
        return [t.anchor_id for t in self.timings if t.needs_repair]


def assemble_voice(result: RenderResult, output_audio: Path, work_dir: Path, crossfade: float = 0.06, pitch: float = 1.0) -> Path:
    """Assemble the placed clips smoothly, then master the track (EQ/compress/limit)."""
    output_audio = Path(output_audio)
    work_dir = Path(work_dir)
    unmastered = work_dir / "voice.unmastered.m4a"
    assemble_track(result.placements, unmastered, total_duration=result.total_duration, crossfade=crossfade)
    master_voice(unmastered, output_audio, pitch=pitch)
    result.voice_track = output_audio
    result.summary["voice_track"] = str(output_audio)
    return output_audio


def _blocks_from_units(units: list[dict], text_mode: str) -> list[TtsBlock]:
    """One TTS block per SENTENCE unit; ``block_id`` = the unit id its rows belong to."""
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
    synth_client: "CapCutTtsClient | None" = None,
) -> RenderResult:
    """Synthesize every sentence, place it by RUN, report per-unit and per-run timing.

    Does NOT assemble the final track (the repair loop re-measures first); call
    :func:`assemble_voice` once timings converge.
    """
    require_ffmpeg()
    timed_units = build_timed_units(speech_rows, config.min_duration, anchors, segments)
    blocks = _blocks_from_units(timed_units, config.text_mode)
    if not blocks:
        return RenderResult(voice_track=None, summary={"blocks": 0, "units_need_repair": 0, "runs_need_expand": 0})

    provider = None
    if synth_client is None:
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

    def raw_path_for(block: TtsBlock) -> Path:
        return raw_audio_path(
            cache_dir, config.provider_url, config.speaker, block,
            speed=config.synth_rate, pitch=config.pitch, volume=config.volume, text_mode=config.text_mode,
        )

    if synth_client is not None:
        from backend.app.services.capcut_tts import synthesize_many
        from backend.app.services.tts import to_provider_scale

        todo = [(raw_path_for(b), b) for b in blocks]
        todo = [(rp, b) for rp, b in todo if not (rp.exists() and rp.stat().st_size > 1024)]
        if todo:
            audios = synthesize_many(
                synth_client,
                [b.text for _, b in todo],
                batch_size=config.batch_size,
                concurrency=config.batch_concurrency,
                rate=config.synth_rate,
                volume=to_provider_scale(config.volume),
            )
            for (rp, _block), audio in zip(todo, audios):
                rp.parent.mkdir(parents=True, exist_ok=True)
                tmp = rp.with_name(f"{rp.stem}.{os.getpid()}-{threading.get_ident()}.build{rp.suffix}")
                tmp.write_bytes(audio)
                trim_silence(tmp)
                speed_clip(tmp, config.synth_rate)
                tmp.replace(rp)

    def ensure_raw(block: TtsBlock) -> Path:
        raw_path = raw_path_for(block)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not (raw_path.exists() and raw_path.stat().st_size > 1024):
            if provider is None:
                raise RuntimeError(f"native synth produced no audio for block {block.block_id}")
            tmp = raw_path.with_name(f"{raw_path.stem}.{os.getpid()}-{threading.get_ident()}.build{raw_path.suffix}")
            tmp.write_bytes(provider.synthesize(block.text))
            trim_silence(tmp)
            speed_clip(tmp, config.synth_rate)
            tmp.replace(raw_path)
        return raw_path

    # 1) raw synth (parallel) and raw durations
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        raw_paths = list(pool.map(ensure_raw, blocks))
        raw_durations = list(pool.map(probe_duration, raw_paths))

    # 2) run-based plan: one tempo per run, chained breaths, run-boundary anchoring
    timeline_config = TimelineConfig(
        run_gap=config.run_gap,
        tempo_floor=config.tempo_floor,
        tempo_ceiling=config.tempo_ceiling,
        breath_min=config.breath_min,
        breath_max=config.breath_max,
        pause_keep=config.pause_keep,
        min_run_fill=config.min_run_fill,
        min_trailing=config.min_trailing,
    )
    pseudo_units = [
        Unit(id=index, segment_ids="", start=block.start, end=block.end, source_zh="")
        for index, block in enumerate(blocks)
    ]
    min_breaths: dict[int, float] = {}
    for index in range(1, len(blocks)):
        previous_text = str(timed_units[index - 1].get("text", "")).rstrip()
        if previous_text.endswith("…"):
            min_breaths[index] = timeline_config.dramatic_breath
        elif previous_text.endswith((".", "!", "?")):
            min_breaths[index] = timeline_config.sentence_breath
        elif previous_text.endswith((",", ";", ":")):
            min_breaths[index] = timeline_config.clause_breath
    plans = plan_timeline(
        pseudo_units,
        {index: duration for index, duration in enumerate(raw_durations)},
        timeline_config,
        min_breaths,
    )
    by_unit: dict[int, tuple[float, float, bool]] = {}
    for plan in plans:
        for placement in plan.placements:
            by_unit[placement.unit_id] = (placement.place_start, placement.tempo, placement.breath_before > 0)

    # 3) tempo-fit each clip with its run's factor (cached per clip+tempo)
    def fit_block(index: int) -> tuple[int, float, Path, float, bool]:
        block = blocks[index]
        raw_path = raw_paths[index]
        place, tempo, glued = by_unit.get(index, (block.start, 1.0, False))
        aligned_key = hashlib.sha256(f"{raw_path.stem}|{round(tempo, 4)}|{config.synth_rate}".encode()).hexdigest()[:24]
        aligned = cache_dir / "aligned" / f"{aligned_key}.m4a"
        playback: float | None = None
        if aligned.exists() and aligned.stat().st_size > 1024:
            try:
                playback = probe_duration(aligned)
            except RuntimeError:
                aligned.unlink(missing_ok=True)
        if playback is None:
            timing = align_audio(raw_path, aligned, raw_durations[index] / tempo, min_tempo=1.0, max_tempo=1.0, fixed_tempo=tempo)
            playback = float(timing["playback_duration"])
        return (index, place, aligned, playback, glued)

    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        fitted = list(pool.map(fit_block, range(len(blocks))))

    # 4) per-unit timing vs source window (overflow drives the repair loop)
    placements = [(place, path, playback, glued) for _index, place, path, playback, glued in fitted]
    played: dict[int, float] = {}
    for index, _place, _path, playback, _glued in fitted:
        anchor_id = int(blocks[index].block_id)
        played[anchor_id] = played.get(anchor_id, 0.0) + playback

    timings: list[AnchorTiming] = []
    anchor_window = {a.id: (a.start, a.end) for a in anchors}
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

    runs = [
        {
            "run_id": plan.run_id,
            "start": plan.start,
            "end": plan.end,
            "fill_ratio": plan.fill_ratio,
            "needs_expand": plan.needs_expand,
            "unit_ids": [p.unit_id for p in plan.placements],
        }
        for plan in plans
    ]
    total_duration = max((b.end for b in blocks), default=0.1)
    summary = {
        "blocks": len(blocks),
        "units": len(timings),
        "units_need_repair": sum(1 for t in timings if t.needs_repair),
        "max_overflow_ms": max((t.overflow_ms for t in timings), default=0),
        "runs": len(runs),
        "runs_need_expand": sum(1 for r in runs if r["needs_expand"]),
        "overflow_pushes_next_voice": False,
        "timeline": summarize(plans, timeline_config),
        "voice_track": None,
        "total_duration": round(total_duration, 3),
    }
    return RenderResult(
        voice_track=None,
        timings=timings,
        summary=summary,
        placements=placements,
        block_starts=[blocks[index].start for index, _place, _path, _play, _glued in fitted],
        runs=runs,
        total_duration=total_duration,
    )
