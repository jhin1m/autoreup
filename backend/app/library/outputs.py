"""One readable folder per dubbed episode: ``data/outputs/<series>/<episode>/``.

The pipeline keeps its files by kind (videos/, subtitles/, tts/, packages/) under job
ids — right for the stages, useless for a person gathering an episode to upload or to
hand to ChatGPT for a thumbnail/title. After every successful render the episode's
deliverables are gathered here under plain names. Big media is hard-linked (no extra
disk); text is written fresh in OUTPUT time so it lines up with the rendered video.

An episode dubbed but not rendered yet (waiting for review) gathers too, for a partner
who renders it themselves: the source video instead of the render, and a render guide
(mix levels, picture slow-downs) in ``thong-tin.md``.

Only the files listed in ``_OWNED`` belong to the app: a re-render overwrites them and
deleting the dub removes them, but anything else the user drops in the folder (their
finished thumbnail, notes) is never touched.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from backend.app.core.config import Settings
from backend.app.domain.artifacts import ArtifactKind
from backend.app.domain.jobs import JobManifest
from backend.app.dubbing.time_warp import TimeWarp
from backend.app.dubbing.tsv_io import read_tsv
from backend.app.library.grouping import episode_number
from backend.app.services.subtitle_formats import load_cues, write_srt

VIDEO = "video.mp4"
SOURCE = "video-goc.mp4"  # only while the episode has no render of its own
VOICE = "voice.m4a"
SUBTITLE = "phu-de.vi.srt"
INFO = "thong-tin.md"
FRAMES = "khung-hinh"
_MARKER = ".autoreup"  # holds the episode id: which episode owns this folder
_OWNED = (VIDEO, SOURCE, VOICE, SUBTITLE, INFO, _MARKER)
_FRAME_COUNT = 8
_LOOSE_SERIES = "Video lẻ"  # jobs made outside the library (no series)
# newest deliverable first: the hardsub is the real one, the others are no-TTS fallbacks
_VIDEO_KINDS = (ArtifactKind.hardsub_mp4, ArtifactKind.dubbed_mp4, ArtifactKind.soft_sub_mp4)
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


@dataclass
class EpisodeInfo:
    """What the folder is named after and what ``thong-tin.md`` tells about it."""

    owner_id: str            # episode id (job id for a loose video): keys the folder
    series_title: str
    label: str               # folder name: "Tập 03", or the title when it has no number
    title: str = ""
    title_vi: str | None = None
    source_url: str = ""
    uploader: str | None = None


def outputs_root(settings: Settings) -> Path:
    return settings.data_dir / "outputs"


def safe_name(text: str, limit: int = 80) -> str:
    """A folder name that works on macOS/Windows: no separators or reserved characters."""
    name = _UNSAFE.sub(" ", text or "").strip(" .")
    name = re.sub(r"\s+", " ", name)[:limit].strip(" .")
    return name or "Không tên"


def episode_label(title: str, title_vi: str | None = None) -> str:
    number = episode_number(title)
    return f"Tập {number:02d}" if number is not None else safe_name(title_vi or title)


def find_folder(settings: Settings, series_title: str, owner_id: str) -> Path | None:
    """The folder an episode was gathered into, if any (found by its marker)."""
    series_dir = outputs_root(settings) / safe_name(series_title)
    if not series_dir.is_dir():
        return None
    for child in series_dir.iterdir():
        if _owner(child) == owner_id:
            return child
    return None


def folder_for(settings: Settings, info: EpisodeInfo) -> Path:
    """Where this episode gathers. Two episodes with the same label (a re-upload of
    "第3集") get distinct folders instead of overwriting each other."""
    existing = find_folder(settings, info.series_title, info.owner_id)
    if existing is not None:
        return existing
    base = outputs_root(settings) / safe_name(info.series_title) / safe_name(info.label)
    if _owner(base) in (None, info.owner_id) and not _has_files(base):
        return base
    return base.with_name(f"{base.name} ({info.owner_id[:6]})")


def gather(settings: Settings, manifest: JobManifest, info: EpisodeInfo) -> Path | None:
    """Gather a job's deliverables into the episode folder: the render when there is
    one, else the source video + voice for someone else to render. None when the job
    has neither a render nor a dub yet (nothing worth gathering)."""
    job_id = manifest.job_id
    rendered = _latest(settings, manifest, _VIDEO_KINDS)
    source = settings.videos_dir / f"{job_id}.mp4"
    cues_path = settings.subtitles_dir / f"{job_id}.vi.display.json"
    if rendered is None and not (source.exists() and cues_path.exists()):
        return None
    folder = folder_for(settings, info)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / _MARKER).write_text(info.owner_id, encoding="utf-8")

    warp = _load_warp(settings, job_id)
    video = rendered or source
    _link(video, folder / (VIDEO if rendered else SOURCE))
    (folder / (SOURCE if rendered else VIDEO)).unlink(missing_ok=True)
    voice = _latest(settings, manifest, (ArtifactKind.voice_track,))
    if voice is not None:
        _link(voice, folder / VOICE)
    else:
        (folder / VOICE).unlink(missing_ok=True)

    if cues_path.exists():
        cues = [cue | {"start": warp.to_output(float(cue["start"])), "end": warp.to_output(float(cue["end"]))} for cue in load_cues(cues_path)]
        write_srt(cues, folder / SUBTITLE)

    # stills from the source (no Vietnamese caption over them); the render when the
    # source was cleaned up
    frames = folder / FRAMES
    if not any(frames.glob("*.jpg")):
        _grab_frames(source if source.exists() else video, frames)

    duration = _probe_duration(video)
    if duration and not rendered:
        duration += warp.extra_seconds()  # the episode's length once slowed as the guide says
    guide = None if rendered else _render_guide(settings, job_id, warp)
    (folder / INFO).write_text(_info_markdown(settings, job_id, info, warp, duration, guide), encoding="utf-8")
    return folder


def remove(settings: Settings, series_title: str, owner_id: str) -> None:
    """Drop the app's files from an episode folder (the dub was deleted). Files the user
    added stay; the folder (and an emptied series folder) goes only when empty."""
    folder = find_folder(settings, series_title, owner_id)
    if folder is None:
        return
    for name in _OWNED:
        (folder / name).unlink(missing_ok=True)
    shutil.rmtree(folder / FRAMES, ignore_errors=True)
    for path in (folder, folder.parent):
        try:
            path.rmdir()
        except OSError:
            break  # not empty: the user's own files live there


def gather_job(settings: Settings, manifest: JobManifest) -> Path | None:
    """Gather a job straight after it succeeded, naming it from the library (or, for a
    video dubbed outside the library, from its download title)."""
    info = None
    if manifest.input.series_id and manifest.input.source_video_id:
        from backend.app.library.db import LibraryDB
        from backend.app.library.store import LibraryStore

        store = LibraryStore(LibraryDB(settings.data_dir / "library.db"))
        series = store.get_series(manifest.input.series_id, with_sources=False)
        episode = store.get_episode(manifest.input.source_video_id)
        if series and episode:
            info = library_info(series.title, episode)
    if info is None:
        meta = manifest.metrics.get("download") or {}
        title = meta.get("title") or manifest.input.url
        info = EpisodeInfo(
            owner_id=manifest.job_id, series_title=_LOOSE_SERIES, label=safe_name(title),
            title=title, source_url=meta.get("webpage_url") or manifest.input.url, uploader=meta.get("uploader"),
        )
    return gather(settings, manifest, info)


def library_info(series_title: str, episode) -> EpisodeInfo:
    return EpisodeInfo(
        owner_id=episode.id, series_title=series_title, label=episode_label(episode.title, episode.title_vi),
        title=episode.title, title_vi=episode.title_vi, source_url=episode.source_url, uploader=episode.uploader,
    )


# ----- helpers -------------------------------------------------------------
def _owner(folder: Path) -> str | None:
    try:
        return (folder / _MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _has_files(folder: Path) -> bool:
    return folder.is_dir() and any(folder.iterdir())


def _latest(settings: Settings, manifest: JobManifest, kinds: tuple[ArtifactKind, ...]) -> Path | None:
    for kind in kinds:
        for art in reversed(manifest.artifacts):
            if art.kind == kind:
                path = settings.data_dir / art.path
                if path.exists():
                    return path
    return None


def _link(src: Path, dst: Path) -> None:
    """Hard link (same disk, no extra space); a copy where linking isn't possible. Always
    replaced: a re-render writes a new file, an old link would keep the stale video."""
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _load_warp(settings: Settings, job_id: str) -> TimeWarp:
    path = settings.tts_dir / f"{job_id}.tts.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return TimeWarp.from_json(data.get("time_warp"))


def _probe_duration(path: Path) -> float | None:
    from backend.app.services.audio import probe_duration

    try:
        return probe_duration(path)
    except (RuntimeError, ValueError, OSError):
        return None


def _grab_frames(video: Path, out_dir: Path) -> None:
    """Evenly spaced full-size stills, as thumbnail references. Best-effort: a missing
    frame never fails the gather."""
    duration = _probe_duration(video)
    if not duration or not shutil.which("ffmpeg"):
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    for index in range(_FRAME_COUNT):
        at = duration * (index + 0.5) / _FRAME_COUNT
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "2", str(out_dir / f"{index + 1:02d}.jpg")],
            capture_output=True, text=True, check=False,
        )


def _clock(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _characters(settings: Settings, job_id: str) -> list[str]:
    try:
        entries = json.loads((settings.subtitles_dir / f"{job_id}.glossary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    lines = []
    for entry in entries:
        if entry.get("kind") != "name" or not entry.get("term_vi"):
            continue
        # the note also carries translator bookkeeping ("gộp 严继明/…"): keep the description
        note = "; ".join(part.strip() for part in str(entry.get("note") or "").split(";") if part.strip() and not part.strip().startswith("gộp"))
        lines.append(f"- {entry['term_vi']} ({entry.get('term_zh', '')})" + (f": {note}" if note else ""))
    return lines


def _transcript(settings: Settings, job_id: str, warp: TimeWarp) -> list[str]:
    """The final Vietnamese lines (post voice-fit), one sentence per line, timed on the
    rendered video so ChatGPT can write chapters from it."""
    speech = settings.subtitles_dir / f"{job_id}.speech.tsv"
    if speech.exists():
        rows = [(float(r["ANCHOR_START"]), r["TEXT"]) for r in read_tsv(speech) if (r.get("TEXT") or "").strip()]
    else:
        cues_path = settings.subtitles_dir / f"{job_id}.vi.display.json"
        rows = [(float(c["start"]), c.get("text", "")) for c in load_cues(cues_path)] if cues_path.exists() else []
    return [f"[{_clock(warp.to_output(start))}] {text.strip()}" for start, text in rows]


def _render_guide(settings: Settings, job_id: str, warp: TimeWarp) -> list[str]:
    """How the app would have rendered this episode, for a partner rendering it in their
    own editor: which file is what, the mix levels, and where the picture is slowed."""
    from backend.app.services.overlay import load_overlay

    overlay = load_overlay(settings.jobs_dir / job_id, job_id)
    bed = getattr(overlay, "bed_volume", None)
    gap = getattr(overlay, "gap_volume", None)
    voice = getattr(overlay, "voice_volume", 1.0)
    lines = [
        f"- `{SOURCE}`: video gốc (còn phụ đề Trung + logo kênh nguồn → cần che).",
        f"- `{VOICE}`: giọng lồng tiếng, đặt từ 00:00 của timeline.",
        f"- `{SUBTITLE}`: phụ đề tiếng Việt, khớp với `{VOICE}`.",
        f"- Âm lượng: giọng {voice:.0%}; âm thanh gốc {settings.dub_bed_volume if bed is None else bed:.0%} khi có lời thoại, "
        f"{settings.dub_gap_volume if gap is None else gap:.0%} ở đoạn không ai nói.",
    ]
    if warp:
        lines.append(f"- Hình phải làm chậm ở {len(warp.segments)} đoạn (giọng và phụ đề đã tính theo hình đã làm chậm), "
                     "mốc theo video gốc:")
        lines += [f"  - {_clock(seg.start)}–{_clock(seg.start + seg.length)}: tốc độ {1 / seg.factor:.2f}x" for seg in warp.segments]
    else:
        lines.append(f"- Không cần đổi tốc độ hình: `{VOICE}` và `{SUBTITLE}` khớp thẳng với `{SOURCE}`.")
    return lines


def _info_markdown(settings: Settings, job_id: str, info: EpisodeInfo, warp: TimeWarp, duration: float | None, guide: list[str] | None = None) -> str:
    facts = [f"- Bộ: {info.series_title}", f"- Tập: {info.label}"]
    if info.title:
        facts.append(f"- Tiêu đề gốc: {info.title}")
    if info.title_vi:
        facts.append(f"- Tiêu đề tiếng Việt: {info.title_vi}")
    if info.source_url:
        facts.append(f"- Nguồn: {info.source_url}" + (f" (kênh {info.uploader})" if info.uploader else ""))
    if duration:
        facts.append(f"- Thời lượng: {_clock(duration)}")
    characters = _characters(settings, job_id) or ["- (chưa có)"]
    transcript = _transcript(settings, job_id, warp) or ["(chưa có)"]
    return "\n".join([
        f"# {info.series_title} — {info.label}",
        "",
        "## Thông tin",
        *facts,
        "",
        "## Nhân vật",
        *characters,
        "",
        *(["## Hướng dẫn render", *guide, ""] if guide else []),
        "## Yêu cầu",
        "Đây là một tập video thuyết minh tiếng Việt cho kênh YouTube. Dựa vào thông tin, nhân vật và lời thoại bên dưới, hãy:",
        "1. Đề xuất 5 tiêu đề YouTube cuốn hút, gây tò mò, mỗi tiêu đề ≤100 ký tự.",
        "2. Viết mô tả: 3-5 câu tóm nội dung hấp dẫn, 1 câu kêu gọi đăng ký, rồi danh sách chapter (mốc thời gian lấy từ lời thoại).",
        "3. Gợi ý 5-8 hashtag tiếng Việt, mỗi tag không có dấu cách.",
        f"4. Mô tả ý tưởng thumbnail: cảnh, nhân vật, biểu cảm, chữ trên ảnh ≤5 từ. Ảnh tham khảo nằm trong thư mục `{FRAMES}/`.",
        "",
        "## Lời thoại (mốc thời gian theo video thành phẩm)",
        *transcript,
        "",
    ])
