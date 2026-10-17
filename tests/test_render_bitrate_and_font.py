from pathlib import Path

import backend.app.infrastructure.ffmpeg_encode as encode
from backend.app.core.config import Settings
from backend.app.pipeline.production import _hardsub_config
from backend.app.services.fonts import default_font, list_fonts, resolve_font
from backend.app.services.overlay import OverlayConfig

AV1_EPISODE = {"codec": "av1", "width": 1920, "height": 1080, "bit_rate": 440_859}


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(root_dir=tmp_path, _env_file=None, **overrides)


def test_the_render_follows_the_source_quality_instead_of_a_flat_8m(monkeypatch, tmp_path):
    monkeypatch.setattr(encode, "probe_video", lambda path: AV1_EPISODE)
    settings = _settings(tmp_path)
    # AV1 needs ~2x the bits in H.264 for the same picture; the old flat 8M was 18x
    assert encode.target_bitrate(settings, Path("ep.mp4")) == 881_718

    monkeypatch.setattr(encode, "probe_video", lambda path: {**AV1_EPISODE, "width": 1280, "height": 720})
    assert encode.target_bitrate(settings, Path("ep.mp4")) == 1_983_865  # upscaled to 1080p

    monkeypatch.setattr(encode, "probe_video", lambda path: {**AV1_EPISODE, "bit_rate": 9_000_000})
    assert encode.target_bitrate(settings, Path("ep.mp4")) == 8_000_000  # capped


def test_a_fixed_or_unknown_bitrate_still_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(encode, "probe_video", lambda path: None)
    assert encode.target_bitrate(_settings(tmp_path), Path("ep.mp4")) == 8_000_000
    assert encode.target_bitrate(_settings(tmp_path, video_bitrate="1500k"), Path("ep.mp4")) == 1_500_000
    assert encode.parse_bitrate("8M") == 8_000_000 and encode.parse_bitrate("900000") == 900_000


def test_the_encoder_args_carry_the_matched_bitrate(monkeypatch, tmp_path):
    monkeypatch.setattr(encode, "probe_video", lambda path: AV1_EPISODE)
    monkeypatch.setattr(encode, "use_hw_encode", lambda settings: True)
    args = encode.video_encode_args(_settings(tmp_path), Path("ep.mp4"))
    assert args[args.index("-b:v") + 1] == "881718"
    monkeypatch.setattr(encode, "use_hw_encode", lambda settings: False)
    args = encode.video_encode_args(_settings(tmp_path), Path("ep.mp4"))
    assert args[args.index("-maxrate") + 1] == "881718" and "-crf" in args


def test_the_overlay_font_is_burned_and_an_unknown_one_falls_back(tmp_path):
    settings = Settings(_env_file=None)  # the real repo: vendored fonts
    anton = next(font["path"] for font in list_fonts(settings) if font["name"] == "Anton")

    overlay = OverlayConfig.from_json(OverlayConfig(font=anton).to_json())
    assert _hardsub_config(settings, overlay).font_path == anton
    # never an arbitrary file from the request
    assert resolve_font(settings, "/etc/passwd") is None
    assert _hardsub_config(settings, OverlayConfig(font="/etc/passwd")).font_path == str(default_font(settings))
    assert _hardsub_config(settings, None).font_path == str(default_font(settings))


def test_only_fonts_with_vietnamese_glyphs_are_offered():
    names = {font["name"] for font in list_fonts(Settings(_env_file=None))}
    assert {"Anton", "Bangers"} <= names


def test_a_render_uses_the_bitrate_picked_in_the_editor(monkeypatch, tmp_path):
    monkeypatch.setattr(encode, "probe_video", lambda path: AV1_EPISODE)
    monkeypatch.setattr(encode, "use_hw_encode", lambda settings: True)
    settings = Settings(_env_file=None)

    def rate(overlay):
        args = _hardsub_config(settings, overlay, Path("ep.mp4")).video_codec_args
        return args[args.index("-b:v") + 1]

    assert rate(OverlayConfig.from_json({"bitrate": "4M"})) == "4000000"
    assert rate(OverlayConfig.from_json({"bitrate": "source"})) == "881718"
    assert rate(OverlayConfig.from_json({"bitrate": ""})) == "881718"  # the app setting
    # junk or out-of-range picks fall back to the app setting instead of breaking ffmpeg
    for junk in ("8M; rm -rf /", "fast", "10k", "900M"):
        assert OverlayConfig.from_json({"bitrate": junk}).bitrate == ""
    assert OverlayConfig.from_json(OverlayConfig(bitrate="1800k").to_json()).bitrate == "1800k"
