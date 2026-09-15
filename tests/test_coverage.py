from backend.app.dubbing.coverage import find_gaps, find_speech_holes, filter_recovered, score_rows, speech_fraction


def test_find_gaps_reports_ranges_without_segments():
    spans = [(0.0, 10.0), (12.0, 20.0), (60.0, 70.0)]
    gaps = find_gaps(spans, duration=100.0, min_gap=5.0)
    assert gaps == [(20.0, 60.0), (70.0, 100.0)]


def test_find_gaps_ignores_short_holes():
    assert find_gaps([(0.0, 10.0), (13.0, 20.0)], duration=24.0, min_gap=5.0) == []


def test_speech_fraction_counts_vad_frames_above_threshold():
    probs = [0.0, 0.9, 0.9, 0.1, 0.8, 0.0]
    packed = (probs, 1.0)
    assert speech_fraction(packed, 0.0, 6.0) == 0.5
    assert speech_fraction(None, 0.0, 6.0) == 0.0


def test_score_rows_prefers_content_and_punishes_loops():
    good = [{"start": 1, "end": 2, "text": "ご視聴ありがとうございました"}]
    loop = [{"start": 1, "end": 2, "text": "不然,不然,不然"} for _ in range(6)]
    empty = [{"start": 1, "end": 2, "text": "I don't think so..."}]
    assert score_rows(good) > score_rows(empty)
    assert score_rows(good) > score_rows(loop)


def test_filter_recovered_keeps_cjk_and_kana_drops_latin_and_loops():
    rows = [
        {"start": 25.0, "end": 27.0, "text": "让我藉尔的"},              # Han -> keep
        {"start": 27.0, "end": 29.0, "text": "どうなってんだよ"},        # kana -> keep
        {"start": 29.0, "end": 31.0, "text": "あなたのことが好きです"},  # duplicate below -> keep this one
        {"start": 31.0, "end": 33.0, "text": "あなたのことが好きです"},  # near-identical loop -> drop
        {"start": 33.0, "end": 35.0, "text": "heading"},                 # latin only -> drop
        {"start": 35.0, "end": 36.0, "text": ""},                        # empty -> drop
    ]
    kept, skipped = filter_recovered(rows)
    assert [r["text"] for r in kept] == ["让我藉尔的", "どうなってんだよ", "あなたのことが好きです"]
    assert skipped == 3


def test_fuse_rails_prefers_ocr_where_captions_exist():
    from backend.app.dubbing.models import AsrSegment
    from backend.app.dubbing.coverage import fuse_rails

    ocr = [AsrSegment(0, 10.0, 12.0, "这是字幕")]
    asr = [
        AsrSegment(0, 10.1, 11.9, "这是声音"),   # overlaps OCR -> dropped
        AsrSegment(1, 20.0, 22.0, "どうなってんだ"),  # no caption -> kept (JP scene)
    ]
    merged, stats = fuse_rails(ocr, asr)
    texts = [seg.text_zh for seg in merged]
    assert "这是字幕" in texts and "どうなってんだ" in texts
    assert "这是声音" not in texts
    assert stats == {"ocr": 1, "asr_kept": 1, "asr_overlapped": 1}
    assert [seg.id for seg in merged] == [0, 1]


def test_speech_coverage_counts_vad_frames_inside_spans():
    from backend.app.dubbing.coverage import speech_coverage

    packed = ([0.0, 0.9, 0.9, 0.1, 0.8, 0.0], 1.0)  # 3 speech frames
    covered, total, ratio = speech_coverage([(0.0, 3.0)], packed)
    assert total == 3.0
    assert covered == 2.0
    assert abs(ratio - 2 / 3) < 1e-9
    assert speech_coverage([(0.0, 1.0)], None)[2] == 0.0


def test_find_speech_holes_catches_short_gaps_only():
    # speech frames at 2.0-3.0 (uncovered, long enough) and a 0.1s blip at 6.0 (too short)
    probs = [0.0] * 100
    for i in range(20, 31):
        probs[i] = 0.9      # 2.0-3.0s
    probs[60] = 0.9         # 6.0s blip
    holes = find_speech_holes([(0.0, 1.5), (3.5, 10.0)], (probs, 0.1), min_seconds=0.8)
    assert holes == [(2.0, 3.0)]


def test_tighten_to_speech_shrinks_a_music_blob_onto_its_real_line():
    from backend.app.dubbing.models import AsrSegment
    from backend.app.dubbing.onset import tighten_to_speech

    # 12s of music with one spoken second at 8.0-9.0: the 2-10 blob must shrink onto it
    probs = [0.0] * 120
    for i in range(80, 90):
        probs[i] = 0.9
    kept, dropped = tighten_to_speech([AsrSegment(0, 2.0, 10.0, "攻撃は単調だ！")], (probs, 0.1))
    assert dropped == 0 and len(kept) == 1
    assert 7.5 <= kept[0].start <= 8.0 and 9.0 <= kept[0].end <= 9.5
    assert kept[0].text_zh == "攻撃は単調だ！"


def test_tighten_to_speech_drops_music_and_scattered_blips():
    from backend.app.dubbing.models import AsrSegment
    from backend.app.dubbing.onset import tighten_to_speech

    probs = [0.0] * 120
    for i in (10, 11, 12, 50, 51, 52):  # two 0.3s blips 4s apart (a card's music)
        probs[i] = 0.9
    kept, dropped = tighten_to_speech([AsrSegment(0, 0.0, 12.0, "ご視聴ありがとうございました")], (probs, 0.1))
    assert kept == [] and dropped == 1
    kept, dropped = tighten_to_speech([AsrSegment(0, 0.0, 1.0, "x")], ([0.0] * 120, 0.1))
    assert kept == [] and dropped == 1


def test_tighten_to_speech_without_vad_is_a_noop():
    from backend.app.dubbing.models import AsrSegment
    from backend.app.dubbing.onset import tighten_to_speech

    segs = [AsrSegment(3, 1.0, 2.0, "x")]
    kept, dropped = tighten_to_speech(segs, None)
    assert kept is segs and dropped == 0


def test_dedupe_overlaps_keeps_tighter_claim_and_trims_partial():
    from backend.app.dubbing.models import AsrSegment
    from backend.app.dubbing.coverage import dedupe_overlaps

    out = dedupe_overlaps([
        AsrSegment(0, 10.0, 30.0, "blob"),
        AsrSegment(1, 12.0, 14.0, "nested"),  # inside the blob: the tighter claim wins
        AsrSegment(2, 28.0, 31.0, "tail"),    # partial: the earlier tail is trimmed off
    ])
    assert [(s.start, s.end, s.text_zh) for s in out] == [(12.0, 14.0, "nested"), (28.0, 31.0, "tail")]
    assert [s.id for s in out] == [0, 1]
    out = dedupe_overlaps([AsrSegment(0, 0.0, 10.0, "a"), AsrSegment(1, 8.0, 12.0, "b")])
    assert [(s.start, s.end) for s in out] == [(0.0, 8.0), (8.0, 12.0)]
