from backend.app.dubbing.coverage import find_gaps, filter_recovered, score_rows, speech_fraction


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
