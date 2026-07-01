"""Shared ffmpeg encoder selection.

Burning subtitles into pixels forces a re-encode (can't `-c copy` when we change the
image). To keep that unavoidable encode cheap we prefer the Apple hardware encoder on
macOS and fall back to libx264 everywhere else. Both hardsub and export share this so
their outputs use identical codec parameters — which is what lets export stream-copy."""
import platform
import shutil
import subprocess
from functools import lru_cache

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


def video_encode_args(settings: Settings) -> list[str]:
    """ffmpeg -c:v ... args for the chosen H.264 encoder."""
    if use_hw_encode(settings):
        # videotoolbox has no CRF; drive quality with a target bitrate.
        return ["-c:v", "h264_videotoolbox", "-b:v", settings.video_bitrate, "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


def canonical_scale_filter(settings: Settings) -> str:
    """Scale/pad/fps filter that makes every hardsub share one resolution + frame rate,
    so a compilation can concat them with `-c copy` instead of re-encoding again."""
    w, h = settings.video_width, settings.video_height
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={settings.video_fps}"
    )
