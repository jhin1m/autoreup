from backend.app.dubbing.text import (
    clean,
    has_han,
    split_text_for_explicit_turns,
    split_text_for_tts,
    strip_explicit_turn_markers,
)
from backend.app.dubbing.weights import vi_speech_weight
from backend.app.dubbing.models import AsrSegment, Anchor, SpeechRow


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


def test_speech_row_roundtrip():
    row = SpeechRow(0, 3, 1.0, 2.5, "4-6", "Xin chào")
    again = SpeechRow.from_row(row.to_row())
    assert again == row
    assert abs(again.anchor_duration - 1.5) < 1e-9


def test_display_cues_cover_each_caption_window():
    from backend.app.dubbing.display import build_display_cues

    # one row per caption: the cue window IS the caption window — no interpolation
    # across segments, no voice window, no hold pass
    segments = [AsrSegment(0, 10.0, 11.0, "甲甲甲"), AsrSegment(1, 14.0, 15.0, "乙乙乙")]
    rows = [
        SpeechRow(0, 0, 10.0, 11.0, "0-0", "phần một"),
        SpeechRow(1, 1, 14.0, 15.0, "1-1", "phần hai"),
    ]
    cues = build_display_cues(rows, segments, min_duration=0.4)
    assert [c["translated"] for c in cues] == ["phần một", "phần hai"]
    assert cues[0]["start"] == 10.0 and cues[0]["end"] == 11.0
    assert cues[1]["start"] == 14.0 and cues[1]["end"] == 15.0
    assert cues[0]["source"] == "甲甲甲" and cues[0]["source_ids"] == "0-0"


def test_display_reflows_long_lines_inside_the_same_caption():
    from backend.app.dubbing.display import build_display_cues

    segments = [AsrSegment(0, 0.0, 6.0, "一整段字幕")]
    rows = [SpeechRow(0, 0, 0.0, 6.0, "0-0", "Một câu dài cần chia thành hai dòng đọc cho dễ, phần còn lại xuống dòng.")]
    cues = build_display_cues(rows, segments, min_duration=0.4, max_chars=42)
    assert len(cues) == 2
    assert cues[0]["start"] == 0.0 and cues[-1]["end"] == 6.0
    assert cues[0]["end"] <= cues[1]["start"] + 1e-6
    assert all(len(c["text"]) <= 42 + 8 for c in cues)


def test_display_closes_micro_gaps_between_captions():
    from backend.app.dubbing.display import build_display_cues

    segments = [AsrSegment(0, 0.0, 2.0, "一"), AsrSegment(1, 2.3, 4.0, "二")]
    rows = [
        SpeechRow(0, 0, 0.0, 2.0, "0-0", "Câu một."),
        SpeechRow(1, 1, 2.3, 4.0, "1-1", "Câu hai."),
    ]
    cues = build_display_cues(rows, segments, min_duration=0.4)
    assert cues[0]["end"] == cues[1]["start"] == 2.3  # 0.3s gap closed (no flicker)
