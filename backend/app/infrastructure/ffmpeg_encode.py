"""Shared ffmpeg encoder selection.

Burning subtitles into pixels forces a re-encode (can't `-c copy` when we change the
image). To keep that unavoidable encode cheap we prefer the Apple hardware encoder on
macOS and fall back to libx264 everywhere else. Both hardsub and export share this so
their outputs use identical codec parameters — which is what lets export stream-copy."""
import json
import platform
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from backend.app.core.config import Settings


@lru_cache(maxsize=1)
def _has_videotoolbox() -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return "h264_videotoolbox" in out


def use_hw_encode(settings: Settings) -> bool:
    return settings.video_hw_encode and platform.system() == "Darwin" and _has_videotoolbox()


def video_encode_args(settings: Settings, source: Path | None = None, bitrate: str = "") -> list[str]:
    """ffmpeg -c:v ... args for the chosen H.264 encoder, sized to ``source``
    (or to ``bitrate`` when the user picked one for this render)."""
    rate = target_bitrate(settings, source, bitrate)
    if use_hw_encode(settings):
        # videotoolbox has no CRF; drive quality with a target bitrate.
        return ["-c:v", "h264_videotoolbox", "-b:v", str(rate), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", str(rate), "-bufsize", str(rate * 2), "-pix_fmt", "yuv420p"]


# H.264 bits needed per source bit for the same picture. Measured on a real AV1 episode
# (60s, SSIM vs the decoded source): videotoolbox at the source's 440k = 0.974 and
# visibly blocky, at 2x (900k) = 0.986 and indistinguishable from the source.
_H264_BITS_PER_SOURCE_BIT = {"av1": 2.0, "vp9": 1.8, "hevc": 1.5, "h264": 1.2}
_MIN_BITRATE = 500_000


def target_bitrate(settings: Settings, source: Path | None = None, bitrate: str = "") -> int:
    """Video bitrate (bit/s) for a re-encode: the source's quality, not a flat number.

    ``bitrate`` (the render's own pick) or else ``video_bitrate``: "source" scales the
    source's own rate by its codec's efficiency (and by the pixel ratio when the
    canonical size upscales), capped at ``video_bitrate_max``; a fixed value like "8M"
    is used as is."""
    ceiling = parse_bitrate(settings.video_bitrate_max)
    choice = bitrate or settings.video_bitrate
    if choice != "source":
        return parse_bitrate(choice)
    probe = probe_video(source) if source else None
    if not probe or not probe["bit_rate"]:
        return ceiling
    factor = _H264_BITS_PER_SOURCE_BIT.get(probe["codec"], 1.5)
    pixels = probe["width"] * probe["height"]
    upscale = max(1.0, settings.video_width * settings.video_height / pixels) if pixels else 1.0
    return int(min(ceiling, max(_MIN_BITRATE, probe["bit_rate"] * factor * upscale)))


BITRATE_RE = re.compile(r"^(source|\d+(\.\d+)?[km]?)$", re.IGNORECASE)
MIN_PICKED_BITRATE, MAX_PICKED_BITRATE = 300_000, 50_000_000


def valid_bitrate(value: str) -> bool:
    """A user-picked rate: "source" or 300k..50M ("1800k", "4M", "2500000")."""
    value = value.strip()
    if not BITRATE_RE.match(value):
        return False
    return value.lower() == "source" or MIN_PICKED_BITRATE <= parse_bitrate(value) <= MAX_PICKED_BITRATE


def parse_bitrate(value: str) -> int:
    """"8M" / "900k" / "1500000" -> bit/s."""
    text = value.strip().lower()
    scale = {"k": 1_000, "m": 1_000_000}.get(text[-1:], 1)
    return int(float(text[:-1] if scale > 1 else text) * scale)


def probe_video(path: Path) -> dict | None:
    """{codec, width, height, bit_rate} of the first video stream, or None.

    Containers like mkv/webm carry no per-stream bit_rate: fall back to the container
    rate minus the audio streams'."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,bit_rate:format=bit_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    rate = _int(video.get("bit_rate"))
    if not rate:
        audio = sum(_int(s.get("bit_rate")) for s in streams if s.get("codec_type") == "audio")
        rate = max(0, _int((data.get("format") or {}).get("bit_rate")) - audio)
    return {"codec": video.get("codec_name", ""), "width": _int(video.get("width")), "height": _int(video.get("height")), "bit_rate": rate}


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def canonical_scale_filter(settings: Settings) -> str:
    """Scale/pad/fps filter that makes every hardsub share one resolution + frame rate,
    so a compilation can concat them with `-c copy` instead of re-encoding again."""
    w, h = settings.video_width, settings.video_height
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={settings.video_fps}"
    )
