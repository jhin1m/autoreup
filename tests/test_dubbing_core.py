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


def test_overlay_geometry_is_source_resolution_independent():
    from backend.app.services.hardsub import HardsubConfig, overlay_geometry

    config = HardsubConfig(out_width=1920, out_height=1080, font_size=58, margin_v=88)
    high = overlay_geometry(3840, 2160, (1700, 120, 3840, 2160), config)   # 4K source
    normal = overlay_geometry(1920, 1080, (850, 60, 1920, 1080), config)   # 1080p source
    assert (high.out_width, high.out_height) == (1920, 1080) == (normal.out_width, normal.out_height)
    assert high.scale == 0.5 and normal.scale == 1.0
    # same caption centre in the OUTPUT for both sources (the band centre maps down)
    assert abs(high.caption_cy - normal.caption_cy) <= 1
    # no band -> the configured bottom margin, in output pixels, for any source size
    assert overlay_geometry(2160, 3840, None, config).caption_cy == 1080 - 88
    # no canonical output -> the overlay is source-sized again
    plain = overlay_geometry(3840, 2160, None, HardsubConfig())
    assert (plain.out_width, plain.out_height, plain.scale) == (3840, 2160, 1.0)


def test_overlay_config_roundtrip_and_normalization():
    from backend.app.services.overlay import CoverRegion, OverlayConfig

    config = OverlayConfig.from_json({
        "caption_mode": "manual",
        "caption_y": 0.9,
        "caption_scale": 1.3,
        "cover": [
            {"x": -0.1, "y": 0.9, "w": 1.4, "h": 0.2, "mode": "nope", "strength": 5, "label": "band"},
        ],
    })
    payload = config.to_json()
    region = payload["cover"][0]
    assert region["x"] == 0.0 and region["y"] == 0.9          # clamped into the frame
    assert region["w"] == 1.0 and region["h"] == 0.1          # width capped at 1 - x
    assert region["mode"] == "blur"                            # unknown mode falls back
    assert region["strength"] == 1.0                           # clamped to 0..1
    assert OverlayConfig.from_json(payload).to_json() == payload  # stable round-trip


def test_overlay_caption_position_follows_band_or_manual():
    from backend.app.services.overlay import CoverRegion, OverlayConfig

    band = OverlayConfig(cover=[CoverRegion(0.0, 0.88, 1.0, 0.1, "blur", 1.0, "band")])
    assert band.caption_cy_fraction() == 0.93                  # centre of the labelled band
    manual = OverlayConfig(caption_mode="manual", caption_y=0.8)
    assert manual.caption_cy_fraction() == 0.8
    assert OverlayConfig().caption_cy_fraction() is None       # nothing to derive -> detector


def test_detect_sub_band_prefers_vision_over_the_pixel_heuristic(monkeypatch):
    """One detector for the rail and the cover: Vision wins, pixels are the fallback.

    Measured on a real episode: Vision found (893, 144, 1920, 1080) while the pixel
    heuristic returned None, so the package left the original subs visible."""
    import backend.app.services.hardsub as hardsub

    monkeypatch.setattr(hardsub, "_detect_band_vision", lambda video: (893, 144, 1920, 1080))
    assert hardsub.detect_sub_band(__import__("pathlib").Path("x.mp4"), hardsub.HardsubConfig()) == (893, 144, 1920, 1080)

    # Vision unavailable -> the pixel heuristic still runs (None here: the file is fake)
    monkeypatch.setattr(hardsub, "_detect_band_vision", lambda video: None)
    assert hardsub.detect_sub_band(__import__("pathlib").Path("x.mp4"), hardsub.HardsubConfig()) is None
