"""Deterministic scene -> chunk grouping (no LLM).

A flat scene map (one scene per few anchors) is the default. An optional LLM
scene pass can supply richer scenes; either way they are normalized (gap-filled
so every anchor is covered), over-long scenes are split, then scenes are greedily
grouped into duration-bounded chunks the translator consumes one at a time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Anchor, Chunk, Scene


@dataclass(slots=True)
class ChunkConfig:
    target_duration: float = 300.0
    min_duration: float = 210.0
    max_duration: float = 360.0
    flat_scene_anchors: int = 6  # anchors per scene in the flat default map


def flat_scene_map(anchors: list[Anchor], config: ChunkConfig | None = None) -> list[Scene]:
    """Deterministic fallback: fixed-size anchor windows, no LLM."""
    config = config or ChunkConfig()
    if not anchors:
        return []
    step = max(1, config.flat_scene_anchors)
    ids = [a.id for a in anchors]
    scenes: list[Scene] = []
    for start in range(0, len(ids), step):
        window = ids[start : start + step]
        scenes.append(
            Scene(
                scene_id=f"S{len(scenes) + 1:03d}",
                start_anchor=window[0],
                end_anchor=window[-1],
                label="Scene",
                summary="Flat deterministic scene window.",
            )
        )
    return scenes


def _anchor_map(anchors: list[Anchor]) -> dict[int, Anchor]:
    return {a.id: a for a in anchors}


def normalize_scenes(scenes: list[Scene], anchors: list[Anchor]) -> list[Scene]:
    """Clamp scenes to the anchor range and insert GAP scenes for any uncovered anchors."""
    amap = _anchor_map(anchors)
    if not amap:
        return []
    first, last = min(amap), max(amap)

    clamped: list[Scene] = []
    for scene in scenes:
        start = max(first, min(last, scene.start_anchor))
        end = max(start, min(last, scene.end_anchor))
        scene.start_anchor, scene.end_anchor = start, end
        clamped.append(scene)
    clamped.sort(key=lambda s: (s.start_anchor, s.end_anchor))

    covered: list[Scene] = []
    cursor = first
    for scene in clamped:
        if scene.end_anchor < cursor:
            continue
        if scene.start_anchor > cursor:
            covered.append(_gap_scene(cursor, scene.start_anchor - 1))
        scene.start_anchor = max(scene.start_anchor, cursor)
        covered.append(scene)
        cursor = scene.end_anchor + 1
    if cursor <= last:
        covered.append(_gap_scene(cursor, last))
    return covered


def _gap_scene(start: int, end: int) -> Scene:
    return Scene(
        scene_id=f"GAP_{start}_{end}",
        start_anchor=start,
        end_anchor=end,
        label="Unmapped ASR beat",
        tone="Infer from ASR",
        summary="Structural gap inserted because the scene map skipped anchors.",
        must_keep="Preserve source facts.",
        pacing="Follow adjacent scene pacing.",
        handoff="Continue source context.",
    )


def _split_long_scene(scene: Scene, amap: dict[int, Anchor], config: ChunkConfig) -> list[Scene]:
    span = amap[scene.end_anchor].end - amap[scene.start_anchor].start
    if span <= config.max_duration:
        return [scene]
    parts: list[Scene] = []
    part_start = scene.start_anchor
    index = 1
    for anchor_id in range(scene.start_anchor, scene.end_anchor + 1):
        elapsed = amap[anchor_id].end - amap[part_start].start
        if anchor_id > part_start and elapsed >= config.target_duration:
            parts.append(_clone_scene(scene, f"{scene.scene_id}.{index}", part_start, anchor_id - 1))
            part_start = anchor_id
            index += 1
    if part_start <= scene.end_anchor:
        parts.append(_clone_scene(scene, f"{scene.scene_id}.{index}", part_start, scene.end_anchor))
    return parts


def _clone_scene(scene: Scene, scene_id: str, start: int, end: int) -> Scene:
    return Scene(
        scene_id=scene_id,
        start_anchor=start,
        end_anchor=end,
        label=scene.label,
        tone=scene.tone,
        summary=scene.summary,
        must_keep=scene.must_keep,
        pacing=scene.pacing,
        handoff=scene.handoff,
    )


def build_chunks(anchors: list[Anchor], scenes: list[Scene], config: ChunkConfig | None = None) -> list[Chunk]:
    config = config or ChunkConfig()
    amap = _anchor_map(anchors)
    if not amap:
        return []

    expanded: list[Scene] = []
    for scene in normalize_scenes(scenes, anchors):
        expanded.extend(_split_long_scene(scene, amap, config))

    chunks: list[Chunk] = []
    current: list[Scene] = []

    def flush() -> None:
        if not current:
            return
        start_anchor = current[0].start_anchor
        end_anchor = current[-1].end_anchor
        chunks.append(
            Chunk(
                id=len(chunks),
                start_anchor=start_anchor,
                end_anchor=end_anchor,
                start=amap[start_anchor].start,
                end=amap[end_anchor].end,
                scene_ids=[s.scene_id for s in current],
                labels=[s.label for s in current if s.label],
            )
        )
        current.clear()

    for scene in expanded:
        if not current:
            current.append(scene)
            continue
        window_start = amap[current[0].start_anchor].start
        candidate_duration = amap[scene.end_anchor].end - window_start
        current_duration = amap[current[-1].end_anchor].end - window_start
        if candidate_duration > config.max_duration or (
            current_duration >= config.min_duration and candidate_duration > config.target_duration
        ):
            flush()
        current.append(scene)
    flush()
    return chunks


def anchors_for_chunk(chunk: Chunk, anchors: list[Anchor]) -> list[Anchor]:
    return [a for a in anchors if chunk.start_anchor <= a.id <= chunk.end_anchor]
