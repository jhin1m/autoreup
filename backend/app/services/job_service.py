import json
import queue
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backend.app.core.config import Settings
from backend.app.domain.artifacts import ArtifactKind
from backend.app.domain.jobs import JobInput, JobManifest, JobState, PipelineConfig, StageRecord, StageStatus
from backend.app.pipeline.production import ProductionPipeline
from backend.app.studio.providers import extract_url


class JobNotFoundError(KeyError):
    pass


class JobBusyError(RuntimeError):
    """Raised when an operation (e.g. delete) needs the job to be stopped first."""


class JobService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pipeline = ProductionPipeline(settings)
        self._lock = threading.Lock()
        self._cancel_requested: set[str] = set()
        # Bounded FIFO queue so picking many episodes at once doesn't spawn one
        # pipeline thread per job (ASR + ffmpeg + TTS × N oversubscribes the box and
        # crashes it). A small pool of workers drains the queue; the rest wait.
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._queued: set[str] = set()  # job_ids waiting or in flight, for de-dup
        self._workers_started = False

    def create(self, job_input: JobInput) -> JobManifest:
        job_id = uuid4().hex[:12]
        job_input = JobInput(
            url=extract_url(job_input.url),
            notes=job_input.notes,
            series_id=job_input.series_id,
            source_video_id=job_input.source_video_id,
        )
        manifest = JobManifest(
            schema_version=self.settings.schema_version,
            job_id=job_id,
            input=job_input,
            config=PipelineConfig(
                content_genre=self.settings.content_genre,
                content_notes=job_input.notes or self.settings.content_notes,
                max_chars=self.settings.max_chars,
                display_max_chars=self.settings.display_max_chars,
                display_max_lines=self.settings.display_max_lines,
                max_cps=self.settings.max_cps,
                target_cps=self.settings.target_cps,
                min_duration=self.settings.min_duration,
                max_duration=self.settings.max_duration,
                min_gap=self.settings.min_gap,
                snap_gap=self.settings.snap_gap,
                tail_out=self.settings.tail_out,
            ),
        )
        self.save(manifest)
        return manifest

    def start_background(self, job_id: str) -> None:
        """Enqueue a job to run on the worker pool (FIFO, bounded concurrency)."""
        with self._lock:
            if job_id in self._queued:
                return
            self._queued.add(job_id)
            self._ensure_workers()
        self._queue.put(job_id)

    def _ensure_workers(self) -> None:
        """Lazily spin up the worker pool on first use. Caller holds ``self._lock``."""
        if self._workers_started:
            return
        self._workers_started = True
        count = max(1, self.settings.max_concurrent_jobs)
        for i in range(count):
            threading.Thread(target=self._worker_loop, name=f"job-worker-{i}", daemon=True).start()

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self.run(job_id)
            except Exception:
                # run() already records failure on the manifest; never let a worker die.
                pass
            finally:
                with self._lock:
                    self._queued.discard(job_id)
                self._queue.task_done()

    def run(self, job_id: str) -> None:
        manifest = self.get(job_id)
        context = self.pipeline.create_context(job_id, manifest.input, manifest.config)
        if self._take_cancel(job_id):
            return self._mark_canceled(manifest)
        manifest.state = JobState.running
        manifest.touch()
        self.save(manifest)

        try:
            for stage in self.pipeline.stages:
                # Cooperative cancellation: stop at the stage boundary. A stage
                # already in flight (ffmpeg/LLM) finishes first, then we bail.
                if self._take_cancel(job_id):
                    return self._mark_canceled(manifest)
                record = StageRecord(name=stage.name, status=StageStatus.running, started_at=datetime.now(timezone.utc))
                manifest.stages.append(record)
                manifest.touch()
                self.save(manifest)

                result = stage.run(context)
                record.status = StageStatus.succeeded
                record.ended_at = datetime.now(timezone.utc)
                record.artifacts = result.artifacts
                record.metrics = result.metrics
                record.logs = result.logs[-200:]
                manifest.artifacts.extend(result.artifacts)
                manifest.metrics[result.name] = result.metrics
                manifest.touch()
                self.save(manifest)

            manifest.state = JobState.succeeded
            manifest.touch()
            self.save(manifest)
        except Exception as exc:
            if manifest.stages:
                manifest.stages[-1].status = StageStatus.failed
                manifest.stages[-1].ended_at = datetime.now(timezone.utc)
                manifest.stages[-1].error = str(exc)
            manifest.state = JobState.failed
            manifest.error = str(exc)
            manifest.touch()
            self.save(manifest)

    def request_cancel(self, job_id: str) -> JobManifest:
        """Ask a pending/running job to stop. No-op for already-finished jobs.

        The running thread owns the state transition (to ``canceled``); here we only
        record the request so a forced delete never races the pipeline thread."""
        manifest = self.get(job_id)
        if manifest.state in {JobState.succeeded, JobState.failed, JobState.canceled}:
            return manifest
        with self._lock:
            self._cancel_requested.add(job_id)
        return manifest

    def _take_cancel(self, job_id: str) -> bool:
        with self._lock:
            if job_id in self._cancel_requested:
                self._cancel_requested.discard(job_id)
                return True
        return False

    def _mark_canceled(self, manifest: JobManifest) -> None:
        if manifest.stages and manifest.stages[-1].status == StageStatus.running:
            manifest.stages[-1].status = StageStatus.failed
            manifest.stages[-1].ended_at = datetime.now(timezone.utc)
            manifest.stages[-1].error = "canceled"
        manifest.state = JobState.canceled
        manifest.touch()
        self.save(manifest)

    def delete(self, job_id: str) -> None:
        """Remove a job and all its artifacts. Refuses while it is still running."""
        manifest = self.get(job_id)  # raises JobNotFoundError
        if manifest.state == JobState.running:
            raise JobBusyError(job_id)
        with self._lock:
            self._cancel_requested.discard(job_id)
        # Drop the per-job artifact files in each data subdir (named "<job_id>...").
        # The shared tts/cache (keyed by text) lives in a "cache" subdir and is left
        # alone. Guard each path stays inside data_dir.
        data_dir = self.settings.data_dir.resolve()
        for sub in (self.settings.packages_dir, self.settings.videos_dir, self.settings.subtitles_dir, self.settings.tts_dir):
            for path in sub.glob(f"{job_id}*"):
                resolved = path.resolve()
                if data_dir not in resolved.parents:
                    continue
                if resolved.is_dir():
                    shutil.rmtree(resolved, ignore_errors=True)
                else:
                    resolved.unlink(missing_ok=True)
        shutil.rmtree(self.settings.jobs_dir / job_id, ignore_errors=True)

    def get(self, job_id: str) -> JobManifest:
        path = self.manifest_path(job_id)
        if not path.exists():
            raise JobNotFoundError(job_id)
        return JobManifest.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, manifest: JobManifest) -> None:
        with self._lock:
            path = self.manifest_path(manifest.job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            tmp.replace(path)

    def manifest_path(self, job_id: str) -> Path:
        return self.settings.jobs_dir / job_id / "manifest.json"

    def artifact_path(self, job_id: str, kind: ArtifactKind) -> tuple[Path, str]:
        manifest = self.get(job_id)
        for artifact in reversed(manifest.artifacts):
            if artifact.kind == kind:
                path = (self.settings.data_dir / artifact.path).resolve()
                data_dir = self.settings.data_dir.resolve()
                if data_dir not in path.parents and path != data_dir:
                    raise FileNotFoundError("Artifact path escapes data directory")
                if not path.exists():
                    raise FileNotFoundError(path)
                return path, artifact.mime_type
        raise FileNotFoundError(kind)

    def list_recent(self, limit: int = 20) -> list[JobManifest]:
        # Newest first by CREATION time — a stable order that doesn't reshuffle when a
        # job updates (sorting by mtime made the list jump around as jobs ran; sorting
        # by the path string buried recent jobs because job_ids are random hex).
        manifests = []
        for path in self.settings.jobs_dir.glob("*/manifest.json"):
            try:
                manifests.append(JobManifest.model_validate_json(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, ValueError):
                continue
        manifests.sort(key=lambda m: m.created_at, reverse=True)
        return manifests[:limit]
