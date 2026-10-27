"""Music-free bed for the dub mix: the source audio minus its soundtrack.

The dub keeps the original audio as a quiet bed (and louder where nobody speaks), so
the source's music — the part Content ID matches — plays through the whole render.
With "remove music" on, the bed becomes the source's mid channel minus Bandit v2's
music estimate (``bandit_model``): voices and sound effects stay, the soundtrack goes.

Speed (an episode can run 10 h), measured on Apple M-series MPS:
- one MID channel instead of two (the side channel sat ~20 dB under the mid on a real
  source, and the bed plays quietly under the dub) — half the work;
- fp16 autocast: x1.3, output within -64 dB of fp32;
- 8 s chunks every 7 s with a 1 s crossfade instead of 50% overlap: x1.75.
About 5-6x realtime; a 1 h episode takes ~11 min. The bed is computed once per job
(``data/audio/<job>.nomusic.m4a``) and reused by every re-render; ``prefetch`` starts
it in the background while the user reviews the episode.

Weights: Bandit v2 "multi" (DnR v3, multilingual; CC-BY-SA 4.0, Zenodo record
12701995). The Mandarin-only checkpoint was rejected: on a real donghua episode it
put the dialogue into "music". The 447 MB Lightning checkpoint is downloaded once and
reduced to the music head (~80 MB) under ``data/models/bandit/``.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np

from backend.app.core.config import Settings
from backend.app.pipeline.contracts import StageCanceled

logger = logging.getLogger(__name__)

MODEL_URL = "https://zenodo.org/api/records/12701995/files/checkpoint-multi.ckpt/content"
_RATE = 48000
_CHUNK = 8 * _RATE      # the model was trained on 8 s excerpts
_FADE = 1 * _RATE       # crossfade between neighbouring chunks
_HOP = _CHUNK - _FADE
_BATCH = 8              # chunks per forward pass (measured fastest on MPS)

Report = Callable[[dict], None]


class _Task:
    """One job's bed: whoever holds ``lock`` computes it, everyone else waits on it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.done = 0.0  # seconds of audio processed, for waiters' progress


_tasks: dict[str, _Task] = {}
_tasks_lock = threading.Lock()
_gpu = threading.Lock()  # one separation at a time: two would only share the GPU
_model = None
_model_lock = threading.Lock()


def wanted(settings: Settings, overlay=None) -> bool:
    """Remove the music for this render: the episode's choice, else the app setting."""
    choice = getattr(overlay, "remove_music", None)
    return settings.dub_remove_music if choice is None else bool(choice)


def bed_path(settings: Settings, job_id: str) -> Path:
    return settings.data_dir / "audio" / f"{job_id}.nomusic.m4a"


def weights_path(settings: Settings) -> Path:
    return settings.data_dir / "models" / "bandit" / "music-multi.pt"


def ensure_bed(settings: Settings, job_id: str, video: Path, cancel: threading.Event | None = None, report: Report | None = None) -> Path:
    """The job's music-free bed, computed now unless it exists (or another thread is
    computing it — then wait for that one). ``cancel`` stops this caller's wait or work."""
    output = bed_path(settings, job_id)
    task = _task(job_id)
    stopped = lambda: cancel is not None and cancel.is_set()  # noqa: E731
    while not task.lock.acquire(timeout=0.5):
        if stopped():
            raise StageCanceled("canceled")
        if report and task.done:
            report({"done": int(task.done), "total": None, "unit": "giây âm thanh"})
    try:
        if output.exists():
            return output
        if report:
            report({"step": "Tách nhạc khỏi âm gốc"})
        model, device = _load_model(settings, report, stopped)
        with _wait_for(_gpu, stopped):
            _separate(model, device, Path(video), output, task, report, stopped)
        return output
    finally:
        task.done = 0.0
        task.lock.release()


def prefetch(settings: Settings, job_id: str, video: Path) -> None:
    """Start the bed in the background (no-op when it exists or is being made)."""
    task = _task(job_id)
    if bed_path(settings, job_id).exists() or task.lock.locked():
        return
    task.stop.clear()

    def run() -> None:
        try:
            ensure_bed(settings, job_id, video, cancel=task.stop)
        except StageCanceled:
            pass
        except Exception:  # the render computes it again and reports the error there
            logger.exception("music removal for %s failed", job_id)

    threading.Thread(target=run, name=f"nomusic-{job_id}", daemon=True).start()


def stop(job_id: str) -> None:
    """Stop a background bed (the job was canceled or deleted)."""
    with _tasks_lock:
        task = _tasks.get(job_id)
    if task is not None:
        task.stop.set()


def remove(settings: Settings, job_id: str) -> None:
    stop(job_id)
    bed_path(settings, job_id).unlink(missing_ok=True)


def _task(job_id: str) -> _Task:
    with _tasks_lock:
        return _tasks.setdefault(job_id, _Task())


class _wait_for:
    """Acquire ``lock`` while still honouring a stop."""

    def __init__(self, lock: threading.Lock, stopped: Callable[[], bool]):
        self.lock, self.stopped = lock, stopped

    def __enter__(self):
        while not self.lock.acquire(timeout=0.5):
            if self.stopped():
                raise StageCanceled("canceled")

    def __exit__(self, *exc):
        self.lock.release()


def _load_model(settings: Settings, report: Report | None, stopped: Callable[[], bool]):
    global _model
    with _model_lock:
        if _model is None:
            try:
                import torch
            except ImportError as exc:
                raise RuntimeError("Bỏ nhạc cần PyTorch: pip install torch") from exc
            from backend.app.services.bandit_model import BanditMusic

            weights = weights_path(settings)
            if not weights.exists():
                _download_weights(weights, report, stopped)
            device = torch.device("mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
            model = BanditMusic()
            model.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
            _model = (model.float().eval().to(device), device)
        return _model


def _download_weights(weights: Path, report: Report | None, stopped: Callable[[], bool]) -> None:
    """Fetch the published checkpoint once and keep only the music head."""
    import torch

    from backend.app.services.bandit_model import music_state_dict

    weights.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = weights.with_suffix(".ckpt.part")
    if report:
        report({"step": "Tải model tách nhạc (447 MB, chỉ lần đầu)"})
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=60) as response, checkpoint.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while block := response.read(1 << 20):
                if stopped():
                    raise StageCanceled("canceled")
                out.write(block)
                done += len(block)
                if report:
                    report({"done": done >> 20, "total": (total >> 20) or None, "unit": "MB"})
        state = music_state_dict(torch.load(checkpoint, map_location="cpu", weights_only=True))
        partial = weights.with_suffix(".part")
        torch.save(state, partial)
        partial.replace(weights)
    finally:
        checkpoint.unlink(missing_ok=True)


def _separate(model, device, video: Path, output: Path, task: _Task, report: Report | None, stopped: Callable[[], bool]) -> None:
    """Stream the source's mid channel through the model and write mid - music.

    Chunks overlap by ``_FADE``: each chunk's music fades in/out over that span with
    complementary sin²/cos² ramps, so every sample's weights sum to 1. The stream is
    front-padded by ``_FADE`` (dropped again) so the first real sample sits after the
    first fade-in."""
    import torch

    from backend.app.services.audio import probe_duration

    duration = probe_duration(video)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part.m4a")
    decoder = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a:0", "-vn", "-af", _mid_filter(video),
         "-ar", str(_RATE), "-f", "f32le", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    # the bed goes back as stereo (both sides = mid) so the mix graph keeps its layout
    encoder = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "f32le", "-ar", str(_RATE), "-ac", "1", "-i", "-",
         "-af", "pan=stereo|c0=c0|c1=c0", "-c:a", "aac", "-b:a", "192k", str(partial)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    ramp = np.sin(np.linspace(0.0, np.pi / 2, _FADE, endpoint=False, dtype=np.float32)) ** 2
    window = np.ones(_CHUNK, np.float32)
    window[:_FADE] = ramp
    window[-_FADE:] = 1.0 - ramp
    pending = np.zeros(_FADE, np.float32)  # front pad
    carry = np.zeros(_FADE, np.float32)
    need = _BATCH * _HOP + _FADE  # samples one batch of chunks spans
    skip, decoded, written, total = _FADE, 0, 0, None
    try:
        while total is None or written < total:
            if stopped():
                raise StageCanceled("canceled")
            while total is None and len(pending) < need:
                data = decoder.stdout.read((need - len(pending)) * 4)
                if not data:
                    total = decoded  # EOF: the rest of the last batch is zero padding
                    break
                samples = np.frombuffer(data[: len(data) // 4 * 4], np.float32)
                decoded += len(samples)
                pending = np.concatenate([pending, samples])
            chunks = [pending[k * _HOP: k * _HOP + _CHUNK] for k in range(_BATCH)]
            batch = np.zeros((_BATCH, 1, _CHUNK), np.float32)
            for k, chunk in enumerate(chunks):
                batch[k, 0, : len(chunk)] = chunk
            with torch.inference_mode(), torch.autocast(device.type, dtype=torch.float16, enabled=device.type != "cpu"):
                music = model(torch.from_numpy(batch).to(device)).float().cpu().numpy()[:, 0]
            bed = []
            for k in range(_BATCH):
                faded = music[k] * window
                faded[:_FADE] += carry
                carry = faded[_HOP:].copy()
                bed.append(batch[k, 0, :_HOP] - faded[:_HOP])
            block = np.concatenate(bed)
            pending = pending[_BATCH * _HOP:]
            if skip:
                block, skip = block[skip:], 0
            if total is not None:
                block = block[: max(0, total - written)]
            encoder.stdin.write(block.astype(np.float32).tobytes())
            written += len(block)
            task.done = written / _RATE
            if report:
                report({"done": int(task.done), "total": int(duration), "unit": "giây âm thanh"})
        encoder.stdin.close()
        if encoder.wait() != 0:
            raise RuntimeError("ffmpeg không ghi được âm nền đã bỏ nhạc: " + encoder.stderr.read().decode(errors="replace")[-500:])
        partial.replace(output)
    finally:
        for process in (decoder, encoder):
            if process.poll() is None:
                process.kill()
            process.wait()
        partial.unlink(missing_ok=True)


def _mid_filter(video: Path) -> str:
    """(L+R)/2 for a stereo source — ``-ac 1`` would mix at +3 dB — or the mono track."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels", "-of", "csv=p=0", str(video)],
        text=True, capture_output=True, check=False,
    )
    channels = int((probe.stdout.strip() or "1").split(",")[0] or 1)
    if channels >= 2:
        return "pan=mono|c0=0.5*c0+0.5*c1"
    return "anull"
