"""Library service — the business logic tying the store to discovery, jobs, and render.

Endpoints call this; it owns the SQLite store, reuses SourceDiscovery to scan channel
links into episodes, JobService to dub them, and ffmpeg to concat dubbed episodes into
one export video. Job state is reconciled lazily into each episode's cached dub_state.
"""

import shutil
import subprocess
import threading
from pathlib import Path

from backend.app.core.config import Settings
from backend.app.domain.artifacts import ArtifactKind
from backend.app.domain.jobs import JobInput, JobState
from backend.app.library.db import LibraryDB
from backend.app.library.models import DubState, Episode, Export, ExportState, Series
from backend.app.library.store import LibraryStore, _new_id
from backend.app.services.cookie_vault import CookieVault
from backend.app.services.job_service import JobService
from backend.app.studio.models import Platform
from backend.app.studio.providers import SourceDiscovery

# Maps a dubbing job's state to the episode dub_state we cache.
_JOB_TO_DUB = {
    JobState.queued: DubState.translating,
    JobState.running: DubState.translating,
    JobState.succeeded: DubState.dubbed,
    JobState.failed: DubState.failed,
    JobState.canceled: DubState.none,
}


class LibraryError(RuntimeError):
    pass


class LibraryService:
    def __init__(self, settings: Settings, job_service: JobService):
        self.settings = settings
        self.jobs = job_service
        self.store = LibraryStore(LibraryDB(settings.data_dir / "library.db"))
        self.discovery = SourceDiscovery(CookieVault(settings.provider_cookies_dir), settings)

    @property
    def exports_dir(self) -> Path:
        return self.settings.data_dir / "exports"

    # ----- series ---------------------------------------------------------
    def create_series(self, title: str, sources: list[dict] | None = None) -> Series:
        series = self.store.create_series(title)
        for src in sources or []:
            self.store.add_source(series.id, Platform(src["platform"]), src["url"], src.get("label"))
        return self.store.get_series(series.id) or series

    def get_series(self, series_id: str) -> Series | None:
        self._reconcile(series_id)
        return self.store.get_series(series_id)

    def list_series(self, q: str = "", limit: int = 60, offset: int = 0):
        return self.store.list_series(q, limit, offset)

    def update_series(self, series_id: str, **changes) -> Series | None:
        return self.store.update_series(series_id, **changes)

    def delete_series(self, series_id: str) -> bool:
        """Cascade: remove every dubbing job + its files, every export file, then the
        series row (its episodes/sources/exports cascade via the FK)."""
        series = self.store.get_series(series_id)
        if not series:
            return False
        for ep in self.store.list_episodes(series_id):
            if ep.dub_job_id:
                try:
                    self.jobs.delete(ep.dub_job_id)
                except Exception:
                    pass
        for export in self.store.list_exports(series_id):
            self._remove_export_file(export)
        return self.store.delete_series(series_id)

    # ----- sources --------------------------------------------------------
    def add_source(self, series_id: str, platform: str, url: str, label: str | None = None):
        return self.store.add_source(series_id, Platform(platform), url, label)

    def delete_source(self, source_id: str) -> bool:
        return self.store.delete_source(source_id)

    def scan(self, series_id: str) -> dict:
        """Scan every source link of the series and upsert the discovered videos as
        episodes (dedup by platform video id). Returns counts."""
        series = self.store.get_series(series_id)
        if not series:
            raise LibraryError("Series không tồn tại")
        added = 0
        for source in series.sources:
            videos = self.discovery.discover(source.url, limit=50)
            episodes = [self._episode_from_source(series_id, v) for v in videos]
            added += self.store.upsert_episodes(series_id, episodes)
            self.store.mark_source_scanned(source.id)
        # Use the newest episode's thumbnail as the series cover if none set yet.
        if not series.cover_url:
            eps = self.store.list_episodes(series_id)
            cover = next((e.thumbnail_url for e in eps if e.thumbnail_url), None)
            if cover:
                self.store.update_series(series_id, cover_url=cover)
        total = len(self.store.list_episodes(series_id))
        return {"added": added, "total": total}

    # ----- episodes -------------------------------------------------------
    def list_episodes(self, series_id: str, dub_state: DubState | None = None) -> list[Episode]:
        self._reconcile(series_id)
        return self.store.list_episodes(series_id, dub_state)

    def translate(self, series_id: str, episode_ids: list[str]) -> list[str]:
        """Create a dubbing job per episode and link it back. Returns created job ids."""
        job_ids: list[str] = []
        for episode_id in episode_ids:
            ep = self.store.get_episode(episode_id)
            if not ep or ep.series_id != series_id:
                continue
            manifest = self.jobs.create(JobInput(url=ep.source_url, series_id=series_id, source_video_id=ep.id))
            self.jobs.start_background(manifest.job_id)
            self.store.set_episode_dub(ep.id, manifest.job_id, DubState.translating)
            job_ids.append(manifest.job_id)
        return job_ids

    # ----- exports --------------------------------------------------------
    def create_export(self, series_id: str, title: str, description: str, hashtags: list[str], episode_job_ids: list[str]) -> Export:
        if not episode_job_ids:
            raise LibraryError("Chọn ít nhất một tập để ghép")
        return self.store.create_export(series_id, title, description, hashtags, episode_job_ids)

    def get_export(self, export_id: str) -> Export | None:
        return self.store.get_export(export_id)

    def list_exports(self, series_id: str) -> list[Export]:
        return self.store.list_exports(series_id)

    def delete_export(self, export_id: str) -> bool:
        export = self.store.get_export(export_id)
        if export:
            self._remove_export_file(export)
        return self.store.delete_export(export_id)

    def start_render(self, export_id: str) -> Export | None:
        export = self.store.get_export(export_id)
        if not export:
            return None
        self.store.update_export(export_id, state=ExportState.rendering.value, error=None)
        threading.Thread(target=self._render, args=(export_id,), daemon=True).start()
        return self.store.get_export(export_id)

    # ----- internals ------------------------------------------------------
    def _render(self, export_id: str) -> None:
        export = self.store.get_export(export_id)
        if not export:
            return
        try:
            inputs = [self._hardsub_path(job_id) for job_id in export.episode_job_ids]
            inputs = [p for p in inputs if p is not None]
            if not inputs:
                raise LibraryError("Không tìm thấy video đã lồng tiếng cho các tập đã chọn")
            out = self.exports_dir / export.series_id / f"{export_id}.mp4"
            work = self.exports_dir / export.series_id / f"{export_id}.work"
            _concat_videos(inputs, out, work)
            rel = str(out.relative_to(self.settings.data_dir))
            self.store.update_export(export_id, state=ExportState.done.value, output_path=rel,
                                     duration=_probe_duration(out))
        except Exception as exc:
            self.store.update_export(export_id, state=ExportState.failed.value, error=str(exc))

    def _hardsub_path(self, job_id: str) -> Path | None:
        try:
            manifest = self.jobs.get(job_id)
        except Exception:
            return None
        for art in reversed(manifest.artifacts):
            if art.kind == ArtifactKind.hardsub_mp4:
                path = self.settings.data_dir / art.path
                return path if path.exists() else None
        return None

    def _remove_export_file(self, export: Export) -> None:
        if export.output_path:
            path = (self.settings.data_dir / export.output_path)
            if path.exists():
                path.unlink(missing_ok=True)
        work = self.exports_dir / export.series_id / f"{export.id}.work"
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)

    def _reconcile(self, series_id: str) -> None:
        """Refresh each translating episode's dub_state from its job's current state."""
        for ep in self.store.list_episodes(series_id):
            if not ep.dub_job_id or ep.dub_state in {DubState.dubbed, DubState.failed}:
                continue
            try:
                manifest = self.jobs.get(ep.dub_job_id)
            except Exception:
                continue
            mapped = _JOB_TO_DUB.get(manifest.state, DubState.translating)
            if mapped != ep.dub_state:
                self.store.set_episode_dub(ep.id, ep.dub_job_id, mapped)

    def _episode_from_source(self, series_id: str, video) -> Episode:
        return Episode(
            id=_new_id(),
            series_id=series_id,
            platform=video.platform,
            platform_video_id=str(video.platform_video_id),
            source_url=video.source_url,
            title=video.title,
            thumbnail_url=video.thumbnail_url,
            duration=video.duration,
            uploader=video.uploader,
        )


def _concat_videos(inputs: list[Path], output: Path, work_dir: Path) -> None:
    """Normalise each episode to a common spec, then concat losslessly.

    Episodes can differ in resolution/codec/fps, so a plain concat would fail or glitch.
    We re-encode each to 1080p / 30fps / h264 + aac (uniform), then stream-copy them
    together with the concat demuxer — straight join, no transition card."""
    if not shutil.which("ffmpeg"):
        raise LibraryError("Cần ffmpeg để ghép video")
    output.parent.mkdir(parents=True, exist_ok=True)
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    normalized: list[Path] = []
    for index, src in enumerate(inputs):
        norm = work_dir / f"{index:03d}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src),
             "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", str(norm)],
            check=True, capture_output=True, text=True,
        )
        normalized.append(norm)

    list_file = work_dir / "concat.txt"
    list_file.write_text("".join(f"file '{p.name}'\n" for p in normalized), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(output)],
        check=True, capture_output=True, text=True, cwd=str(work_dir),
    )
    shutil.rmtree(work_dir, ignore_errors=True)


def _probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return round(float(out), 2)
    except Exception:
        return None
