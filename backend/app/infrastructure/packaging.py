import shutil
import subprocess
from pathlib import Path

from backend.app.domain.artifacts import Artifact, ArtifactKind
from backend.app.infrastructure.process import run_command
from backend.app.services.subtitle_formats import load_cues, write_vtt


MIME_BY_KIND = {
    ArtifactKind.subtitle_vtt: "text/vtt",
    ArtifactKind.pipeline_audit: "application/json",
    ArtifactKind.voice_track: "audio/mp4",
    ArtifactKind.soft_sub_mp4: "video/mp4",
    ArtifactKind.dubbed_mp4: "video/mp4",
    ArtifactKind.hardsub_mp4: "video/mp4",
    ArtifactKind.zip_package: "application/zip",
}


def package_outputs(video: Path, subtitle_json: Path, subtitle_srt: Path, packages_dir: Path, root_data_dir: Path, voice_track: Path | None = None, bed_volume: float = 0.18, hardsub: bool = False, hardsub_config=None) -> tuple[list[Artifact], list[str]]:
    packages_dir.mkdir(parents=True, exist_ok=True)
    stem = subtitle_json.stem.replace(".final", "")
    vtt_path = packages_dir / f"{stem}.vtt"
    softsub_path = packages_dir / f"{stem}.softsub.mp4"
    dubbed_path = packages_dir / f"{stem}.dubbed.mp4"
    hardsub_path = packages_dir / f"{stem}.hardsub.mp4"
    logs: list[str] = []
    artifacts: list[Artifact] = []

    cues = load_cues(subtitle_json)
    write_vtt(cues, vtt_path)
    artifacts.append(_artifact(ArtifactKind.subtitle_vtt, vtt_path, root_data_dir))

    # The final deliverable is the hardsub MP4: dubbed audio + the original burned-in
    # subtitle covered + the Vietnamese subtitle burned over it. No softsub/dubbed.
    if shutil.which("ffmpeg") and voice_track and voice_track.exists():
        from backend.app.services.hardsub import build_hardsub

        try:
            summary = build_hardsub(video, cues, voice_track, hardsub_path, packages_dir / f"{stem}.hswork", hardsub_config)
            logs.append(f"hardsub: {summary}")
            artifacts.append(_artifact(ArtifactKind.hardsub_mp4, hardsub_path, root_data_dir))
        except Exception as exc:  # don't fail the whole job on a render hiccup
            logs.append(f"hardsub failed: {exc}")
    elif shutil.which("ffmpeg"):
        run_command([
            "ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(subtitle_srt),
            "-c", "copy", "-c:s", "mov_text", "-metadata:s:s:0", "language=vie", str(softsub_path),
        ], cwd=root_data_dir.parent)
        artifacts.append(_artifact(ArtifactKind.soft_sub_mp4, softsub_path, root_data_dir))
    else:
        logs.append("ffmpeg not found; skipped video render")

    # No zip bundle: nothing downstream consumes it — the deliverable is the hardsub
    # MP4 itself, and zipping duplicated ~437MB of already-compressed video per episode.
    return artifacts, logs


def _artifact(kind: ArtifactKind, path: Path, root_data_dir: Path) -> Artifact:
    return Artifact(
        kind=kind,
        path=str(path.relative_to(root_data_dir)),
        mime_type=MIME_BY_KIND[kind],
        size_bytes=path.stat().st_size if path.exists() else None,
    )


def dubbed_command(video: Path, voice_track: Path, subtitle_srt: Path, output: Path, bed_volume: float = 0.18) -> list[str]:
    if has_audio_stream(video):
        return [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(video),
            "-i", str(voice_track),
            "-i", str(subtitle_srt),
            # Keep the original audio as a QUIET, steady bed under the (already
            # mastered) Vietnamese voice — no sidechain ducking, which pumps. Bed
            # level is configurable; a dub keeps the original low so the voice leads.
            "-filter_complex",
            f"[0:a:0]aresample=44100,volume={bed_volume:.3f}[bed];"
            "[1:a:0]aresample=44100,volume=1.0[vo];"
            "[bed][vo]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "volume=0.95,alimiter=limit=0.95[aout]",
            "-map", "0:v:0",
            "-map", "[aout]",
            "-map", "2:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-c:s", "mov_text",
            "-metadata:s:s:0", "language=vie",
            "-shortest",
            str(output),
        ]
    return [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(video),
        "-i", str(voice_track),
        "-i", str(subtitle_srt),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-map", "2:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-c:s", "mov_text",
        "-metadata:s:s:0", "language=vie",
        "-shortest",
        str(output),
    ]


def has_audio_stream(video: Path) -> bool:
    if not shutil.which("ffprobe"):
        return True
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(video)],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())
