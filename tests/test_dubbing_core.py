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


def test_display_cues_split_at_clauses_and_follow_segments():
    from backend.app.dubbing.display import build_display_cues

    # Anchor spans 2 segments with a gap between them. A short line stays WHOLE
    # (one readable cue, no needless flicker); two sentences split at the sentence
    # end and the second is timed near the second segment (14s), not the middle of
    # the anchor window — and words are never broken across cues.
    segments = [AsrSegment(0, 10.0, 11.0, "甲甲甲"), AsrSegment(1, 14.0, 15.0, "乙乙乙")]
    anchor = Anchor(0, 10.0, 15.0, "0-1", "甲甲甲乙乙乙")

    short = [SpeechRow(0, 0, 10.0, 15.0, "0-1", "phần một ba, phần hai bốn")]
    cues = build_display_cues(short, 0.4, [anchor], segments)
    assert [c["translated"] for c in cues] == ["phần một ba, phần hai bốn"]

    # sentence 2 sits past the halfway char mark, so its content belongs to segment 2
    two_sentences = [SpeechRow(0, 0, 10.0, 15.0, "0-1", "Câu đầu tiên nói khá là dài dòng nhé. Câu sau.")]
    cues = build_display_cues(two_sentences, 0.4, [anchor], segments)
    assert len(cues) == 2
    assert cues[0]["translated"] == "Câu đầu tiên nói khá là dài dòng nhé."
    assert cues[1]["translated"] == "Câu sau."
    assert cues[1]["start"] >= 13.0
    assert cues[0]["end"] <= cues[1]["start"] + 1e-6


def test_display_holds_the_last_line_until_the_source_caption_ends():
    from backend.app.dubbing.display import build_display_cues

    # a compact line (voice 3s) under a 9s source caption: the subtitle must stay up
    # while the caption is on screen, not blink off 6s early
    segments = [AsrSegment(0, 0.0, 9.0, "一整段字幕")]
    anchor = Anchor(0, 0.0, 9.0, "0-0", "一整段字幕")
    rows = [SpeechRow(0, 0, 0.0, 9.0, "0-0", "Một câu ngắn.")]
    cues = build_display_cues(rows, 0.4, [anchor], segments, voice_windows={0.0: (0.0, 3.0)})
    assert len(cues) == 1
    assert cues[0]["end"] == 9.0


def test_display_hold_never_overlaps_the_next_cue():
    from backend.app.dubbing.display import build_display_cues

    # unit 0's caption runs to 9s but unit 1's voice starts at 6s: the hold is clamped
    segments = [AsrSegment(0, 0.0, 9.0, "一二"), AsrSegment(1, 9.0, 12.0, "三四")]
    anchors = [Anchor(0, 0.0, 9.0, "0-0", "一二"), Anchor(1, 9.0, 12.0, "1-1", "三四")]
    rows = [
        SpeechRow(0, 0, 0.0, 9.0, "0-0", "Câu một."),
        SpeechRow(1, 1, 9.0, 12.0, "1-1", "Câu hai."),
    ]
    cues = build_display_cues(rows, 0.4, anchors, segments, voice_windows={0.0: (0.0, 3.0), 9.0: (6.0, 9.5)})
    assert len(cues) == 2
    assert cues[0]["end"] == cues[1]["start"] == 6.0  # hold clamped at the next cue
    assert cues[1]["end"] == 12.0                    # last cue holds to its own caption end
