import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

# yt-dlp used as a LIBRARY retries nothing (the CLI's "10 retries" default lives in its
# option parser): one dropped connection killed a 2h24m Bilibili download 6 MB in. Each
# retry resumes the .part file, and a long download over a flaky link can drop many
# times, so the budget is generous; 4xx errors are never retried by yt-dlp anyway.
_RETRIES = 50
_RETRY_SLEEP_CAP = 15.0  # seconds; backoff 1, 2, 4, 8, 15, 15, ...
_SOCKET_TIMEOUT = 15


def _retry_sleep(n: int) -> float:
    return min(2.0 ** n, _RETRY_SLEEP_CAP)


class _ProgressRelay:
    """Turn yt-dlp's per-file progress hooks into one overall progress dict.

    A DASH source (Bilibili) downloads video and audio as separate files, so bytes are
    summed across files; the total grows when the next file starts."""

    def __init__(self, on_progress: Callable[[dict], None]):
        self.on_progress = on_progress
        self.files: dict[str, tuple[int, int | None]] = {}

    def __call__(self, d: dict) -> None:
        if d.get("status") not in {"downloading", "finished"}:
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        downloaded = d.get("downloaded_bytes") or 0
        self.files[d.get("filename") or ""] = (int(downloaded), int(total) if total else None)
        totals = [t for _, t in self.files.values()]
        self.on_progress({
            "downloaded_bytes": sum(b for b, _ in self.files.values()),
            "total_bytes": sum(totals) if all(totals) else None,
            "speed": d.get("speed") if d.get("status") == "downloading" else None,
            "eta": d.get("eta") if d.get("status") == "downloading" else None,
        })


class DownloadError(RuntimeError):
    pass


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    modal_id = parse_qs(parsed.query).get("modal_id", [None])[0]
    if parsed.netloc.endswith("douyin.com") and modal_id:
        return f"https://www.douyin.com/video/{modal_id}"
    return url


class YtDlpDownloader:
    def __init__(self, videos_dir: Path):
        self.videos_dir = videos_dir

    def download(
        self,
        url: str,
        cookies_file: str | None = None,
        video_id: str | None = None,
        cookie_header: str | None = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        try:
            import yt_dlp
        except ImportError as exc:
            raise DownloadError("yt-dlp is not installed. Run: pip install -r requirements.txt") from exc

        normalized = normalize_url(url)
        video_id = video_id or uuid4().hex[:12]
        output_template = str(self.videos_dir / f"{video_id}.%(ext)s")
        options: dict = {
            "outtmpl": output_template,
            "format": "bv*+ba/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": _RETRIES,
            "fragment_retries": _RETRIES,
            "retry_sleep_functions": {"http": _retry_sleep, "fragment": _retry_sleep},
            # A stalled read (no bytes) surfaces as a retryable error after this many
            # seconds instead of hanging the download.
            "socket_timeout": _SOCKET_TIMEOUT,
        }
        relay = _ProgressRelay(on_progress) if on_progress is not None else None
        if relay is not None:
            options["progress_hooks"] = [relay]
        if cookies_file:
            options["cookiefile"] = cookies_file
        if cookie_header:
            options["http_headers"] = {
                "Cookie": cookie_header,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Referer": "https://www.douyin.com/",
            }

        cdn_host = None
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                if urlparse(normalized).netloc.endswith("bilibili.com"):
                    info, cdn_host, filename = self._download_bilibili(ydl, normalized, self.videos_dir / f"{video_id}.mp4", relay)
                else:
                    info, filename = ydl.extract_info(normalized, download=True), None
                filename = filename or Path(ydl.prepare_filename(info)).with_suffix(".mp4")
                if not filename.exists():
                    candidates = sorted(self.videos_dir.glob(f"{video_id}.*"))
                    if not candidates:
                        raise DownloadError("Download finished but output file was not found")
                    filename = candidates[0]
        except Exception as exc:
            raise DownloadError(str(exc)) from exc

        return {
            "id": video_id,
            "title": info.get("title") or video_id,
            "duration": info.get("duration"),
            "thumbnail_url": info.get("thumbnail"),
            "uploader": info.get("uploader") or info.get("creator") or info.get("channel"),
            "path": filename,
            "webpage_url": info.get("webpage_url") or normalized,
            "cdn_host": cdn_host,
        }

    @staticmethod
    def _download_bilibili(ydl, url: str, output: Path, relay: "_ProgressRelay | None") -> tuple[dict, str | None, Path | None]:
        """Extract, move the streams to the fastest CDN host, then fetch each stream
        (DASH video + audio) over many connections — a range the chosen host fails to
        serve falls back to the other probed hosts — and merge them into ``output``.
        Streams that are not plain HTTP, or a server without range support, fall back to
        yt-dlp's own single-connection download (the returned path is then None)."""
        from backend.app.infrastructure import parallel_download
        from backend.app.infrastructure.bilibili_cdn import host_variants, retarget_formats

        raw = ydl.extract_info(url, download=False, process=False)
        picked = retarget_formats(raw) if raw.get("_type", "video") == "video" else None
        host, speeds = picked or (None, {})
        info = ydl.process_ie_result(raw, download=False)
        streams = info.get("requested_formats") or [info]
        if any(s.get("protocol") not in {"http", "https"} or not s.get("url") for s in streams):
            return ydl.process_ie_result(info, download=True), host, None
        paths = []
        try:
            for stream in streams:
                path = output.with_name(f"{output.stem}.f{stream['format_id']}.{stream['ext']}")
                urls = host_variants(stream["url"], speeds)
                parallel_download.download(urls, path, stream.get("http_headers") or {}, _byte_reporter(relay, path))
                paths.append(path)
        except parallel_download.RangeUnsupported:
            return ydl.process_ie_result(info, download=True), host, None
        _merge(paths, output)
        return info, host, output


def _byte_reporter(relay: "_ProgressRelay | None", path: Path):
    """Feed parallel-download byte counts into the yt-dlp-shaped progress relay."""
    if relay is None:
        return None

    def report(downloaded: int, total: int, speed: float | None) -> None:
        finished = downloaded >= total and speed is None
        relay({
            "status": "finished" if finished else "downloading",
            "filename": str(path),
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "speed": speed,
            "eta": int((total - downloaded) / speed) if speed else None,
        })

    return report


def _merge(streams: list[Path], output: Path) -> None:
    """Mux the separately downloaded streams into one mp4 (stream copy, no re-encode)."""
    if len(streams) == 1 and streams[0].suffix == ".mp4":
        streams[0].replace(output)
        return
    if not shutil.which("ffmpeg"):
        raise DownloadError("ffmpeg is required to merge the video and audio streams")
    tmp = output.with_name(output.stem + ".merging.mp4")
    command = ["ffmpeg", "-y", "-v", "error"]
    for stream in streams:
        command += ["-i", str(stream)]
    for index in range(len(streams)):
        command += ["-map", f"{index}"]
    command += ["-c", "copy", str(tmp)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"ffmpeg merge failed: {result.stderr.strip()[-500:]}")
    tmp.replace(output)
    for stream in streams:
        stream.unlink(missing_ok=True)
