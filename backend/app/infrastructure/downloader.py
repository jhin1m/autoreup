from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4


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

    def download(self, url: str, cookies_file: str | None = None, video_id: str | None = None, cookie_header: str | None = None) -> dict:
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
        }
        if cookies_file:
            options["cookiefile"] = cookies_file
        if cookie_header:
            options["http_headers"] = {
                "Cookie": cookie_header,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Referer": "https://www.douyin.com/",
            }

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(normalized, download=True)
                filename = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
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
        }
