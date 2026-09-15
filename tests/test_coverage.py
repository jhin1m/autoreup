from backend.app.dubbing.coverage import find_gaps, filter_recovered, speech_fraction


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


def test_filter_recovered_drops_latin_artifacts_and_loops():
    rows = [
        {"start": 25.0, "end": 27.0, "text": "让我藉尔的"},          # keep
        {"start": 27.0, "end": 29.0, "text": "召喚的数 heading"},    # han present but loop of next? no -> keep
        {"start": 29.0, "end": 31.0, "text": "你会有吗"},            # keep
        {"start": 31.0, "end": 33.0, "text": "你会有吗"},            # loop -> drop
        {"start": 33.0, "end": 35.0, "text": "heading"},             # latin only -> drop
        {"start": 35.0, "end": 36.0, "text": ""},                    # empty -> drop
    ]
    kept, skipped = filter_recovered(rows)
    assert [r["text"] for r in kept] == ["让我藉尔的", "召喚的数 heading", "你会有吗"]
    assert skipped == 3
