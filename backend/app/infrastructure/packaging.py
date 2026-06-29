import shutil
import subprocess
import zipfile
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
    ArtifactKind.zip_package: "application/zip",
}


def package_outputs(video: Path, subtitle_json: Path, subtitle_srt: Path, packages_dir: Path, root_data_dir: Path, voice_track: Path | None = None) -> tuple[list[Artifact], list[str]]:
    packages_dir.mkdir(parents=True, exist_ok=True)
    stem = subtitle_json.stem.replace(".final", "")
    vtt_path = packages_dir / f"{stem}.vtt"
    softsub_path = packages_dir / f"{stem}.softsub.mp4"
    dubbed_path = packages_dir / f"{stem}.dubbed.mp4"
    zip_path = packages_dir / f"{stem}.zip"
    logs: list[str] = []
    artifacts: list[Artifact] = []

    cues = load_cues(subtitle_json)
    write_vtt(cues, vtt_path)
    artifacts.append(_artifact(ArtifactKind.subtitle_vtt, vtt_path, root_data_dir))

    if shutil.which("ffmpeg"):
        ffmpeg_logs = run_command([
            "ffmpeg", "-y", "-v", "error",
            "-i", str(video),
            "-i", str(subtitle_srt),
            "-c", "copy",
            "-c:s", "mov_text",
            "-metadata:s:s:0", "language=vie",
            str(softsub_path),
        ], cwd=root_data_dir.parent)
        logs.extend(ffmpeg_logs)
        artifacts.append(_artifact(ArtifactKind.soft_sub_mp4, softsub_path, root_data_dir))
        if voice_track and voice_track.exists():
            dubbed_logs = run_command(dubbed_command(video, voice_track, subtitle_srt, dubbed_path), cwd=root_data_dir.parent)
            logs.extend(dubbed_logs)
            artifacts.append(_artifact(ArtifactKind.dubbed_mp4, dubbed_path, root_data_dir))
    else:
        logs.append("ffmpeg not found; skipped soft-sub MP4")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in (video, subtitle_json, subtitle_srt, vtt_path):
            archive.write(path, arcname=path.name)
        if voice_track and voice_track.exists():
            archive.write(voice_track, arcname=voice_track.name)
        if softsub_path.exists():
            archive.write(softsub_path, arcname=softsub_path.name)
        if dubbed_path.exists():
            archive.write(dubbed_path, arcname=dubbed_path.name)
    artifacts.append(_artifact(ArtifactKind.zip_package, zip_path, root_data_dir))
    return artifacts, logs


def _artifact(kind: ArtifactKind, path: Path, root_data_dir: Path) -> Artifact:
    return Artifact(
        kind=kind,
        path=str(path.relative_to(root_data_dir)),
        mime_type=MIME_BY_KIND[kind],
        size_bytes=path.stat().st_size if path.exists() else None,
    )


def dubbed_command(video: Path, voice_track: Path, subtitle_srt: Path, output: Path) -> list[str]:
    if has_audio_stream(video):
        return [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(video),
            "-i", str(voice_track),
            "-i", str(subtitle_srt),
            # Keep the original audio as a low, steady bed and lay the (already
            # mastered) Vietnamese voice over it — no sidechain ducking, which
            # pumps; this is the mix the proven probe pipeline used.
            "-filter_complex",
            "[0:a:0]aresample=44100,volume=0.38[bed];"
            "[1:a:0]aresample=44100,volume=1.0[vo];"
            "[bed][vo]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "volume=0.92,alimiter=limit=0.95[aout]",
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
