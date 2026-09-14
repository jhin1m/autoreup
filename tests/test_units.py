from backend.app.dubbing.models import AsrSegment
from backend.app.dubbing.units import UnitConfig, build_units


def _seg(i: int, start: float, end: float, text: str) -> AsrSegment:
    return AsrSegment(id=i, start=start, end=end, text_zh=text)


def test_units_split_on_pause():
    segments = [
        _seg(0, 0.0, 2.0, "第一句话"),
        _seg(1, 2.1, 4.0, "继续说完"),
        _seg(2, 5.0, 7.0, "第二句话开始了"),
        _seg(3, 7.1, 9.0, "继续第二句"),
    ]
    units = build_units(segments, UnitConfig(gap_cut=0.45))
    assert [u.source_zh for u in units] == ["第一句话继续说完", "第二句话开始了继续第二句"]
    assert [u.segment_ids for u in units] == ["0-1", "2-3"]
    assert (units[0].start, units[0].end) == (0.0, 4.0)


def test_units_split_on_punctuation_even_without_pause():
    segments = [
        _seg(0, 0.0, 2.0, "他说完了。"),
        _seg(1, 2.0, 4.0, "然后离开了"),
    ]
    units = build_units(segments)
    assert [u.source_zh for u in units] == ["他说完了。", "然后离开了"]


def test_units_split_on_length_and_never_exceed_hard_max():
    segments = [_seg(i, i * 1.0, i * 1.0 + 0.9, "字" * 25) for i in range(8)]
    units = build_units(segments, UnitConfig(max_chars=60, hard_max_chars=75))
    assert all(len(u.source_zh) <= 75 for u in units)
    assert len(units) >= 2


def test_tiny_fragment_merges_forward():
    segments = [
        _seg(0, 0.0, 1.0, "嗯"),          # 1 char — stub
        _seg(1, 1.1, 3.0, "我知道了"),
    ]
    units = build_units(segments, UnitConfig(min_chars=6))
    assert len(units) == 1
    assert units[0].source_zh == "嗯我知道了"


def test_units_keep_contiguous_timing_and_ids():
    segments = [_seg(i, i * 2.0, i * 2.0 + 1.8, f"句子{i}") for i in range(4)]
    units = build_units(segments)
    assert units[0].start == segments[0].start
    assert units[-1].end == segments[-1].end
    assert units[0].segment_ids.split("-")[0] == "0"
