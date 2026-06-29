from backend.app.dubbing.text import (
    clean,
    has_han,
    split_text_for_explicit_turns,
    split_text_for_tts,
    strip_explicit_turn_markers,
)
from backend.app.dubbing.weights import vi_speech_weight
from backend.app.dubbing.models import AsrSegment, Anchor, SpeechRow
from backend.app.dubbing.rail import AnchorConfig, build_anchors
from backend.app.dubbing.chunking import ChunkConfig, build_chunks, flat_scene_map


def test_clean_collapses_whitespace():
    assert clean("  a\n  b\t c ") == "a b c"
    assert clean(None) == ""


def test_has_han():
    assert has_han("你好 xin chào")
    assert not has_han("xin chào")


def test_strip_explicit_turn_markers():
    assert strip_explicit_turn_markers("a ⟂ b") == "a b"


def test_split_explicit_turns_breaks_on_marker():
    parts = split_text_for_explicit_turns("Ngươi là ai ⟂ Ta là Giang Thu", 60)
    assert parts == ["Ngươi là ai", "Ta là Giang Thu"]


def test_split_for_tts_keeps_short_lines_whole():
    assert split_text_for_tts("Một câu ngắn.", 60) == ["Một câu ngắn."]


def test_vi_speech_weight_monotonic_and_numbers():
    short = vi_speech_weight("ngắn")
    long = vi_speech_weight("đây là một câu dài hơn nhiều với nhiều âm tiết")
    assert long > short
    # numbers expand to spoken syllables, so a big number weighs more than its digits
    assert vi_speech_weight("320 ức linh thạch") > vi_speech_weight("ba ức")


def test_build_anchors_groups_segments_and_preserves_rail():
    segments = [AsrSegment(i, i * 1.0, i * 1.0 + 0.9, f"字{i}") for i in range(10)]
    anchors = build_anchors(segments, AnchorConfig(max_anchor_segments=3, max_anchor_duration=999, max_anchor_source_chars=999))
    # 10 segments / 3 per anchor -> 4 anchors
    assert len(anchors) == 4
    # source ids cover the full segment range with no gaps
    assert anchors[0].source_id_range() == (0, 2)
    assert anchors[-1].source_id_range() == (9, 9)
    # anchor timing spans its segments
    assert anchors[0].start == 0.0
    assert anchors[0].end == segments[2].end


def test_build_chunks_covers_every_anchor_via_flat_map():
    segments = [AsrSegment(i, i * 2.0, i * 2.0 + 1.8, f"字{i}") for i in range(30)]
    anchors = build_anchors(segments, AnchorConfig(max_anchor_segments=2, max_anchor_duration=999, max_anchor_source_chars=999))
    scenes = flat_scene_map(anchors, ChunkConfig(flat_scene_anchors=4))
    chunks = build_chunks(anchors, scenes, ChunkConfig(target_duration=20, min_duration=10, max_duration=30))
    # chunks cover the full anchor range contiguously
    assert chunks[0].start_anchor == anchors[0].id
    assert chunks[-1].end_anchor == anchors[-1].id
    covered = []
    for c in chunks:
        covered.extend(range(c.start_anchor, c.end_anchor + 1))
    assert covered == [a.id for a in anchors]


def test_speech_row_roundtrip():
    row = SpeechRow(0, 3, 1.0, 2.5, "4-6", "Xin chào")
    again = SpeechRow.from_row(row.to_row())
    assert again == row
    assert abs(again.anchor_duration - 1.5) < 1e-9


def test_display_cues_follow_segment_timeline():
    from backend.app.dubbing.display import build_display_cues

    # Anchor spans 2 segments with a gap between them; the second cue should be
    # timed to the second segment, not the middle of the anchor window.
    segments = [AsrSegment(0, 10.0, 11.0, "甲甲甲"), AsrSegment(1, 14.0, 15.0, "乙乙乙")]
    anchor = Anchor(0, 10.0, 15.0, "0-1", "甲甲甲乙乙乙")
    rows = [
        SpeechRow(0, 0, 10.0, 15.0, "0-1", "phần một ba ký"),
        SpeechRow(1, 0, 10.0, 15.0, "0-1", "phần hai ba ký"),
    ]
    cues = build_display_cues(rows, 0.4, [anchor], segments)
    assert len(cues) == 2
    # second cue starts near the second segment (14s), not ~12.5s (window midpoint)
    assert cues[1]["start"] >= 13.0
    # cues are monotonic and non-overlapping
    assert cues[0]["end"] <= cues[1]["start"] + 1e-6
