"""Douyin downloader via a headless Chromium browser (no yt-dlp).

Douyin's web API requires a JS-generated request signature that yt-dlp can't
produce, so we let Douyin's own JS run: a headless Chromium (Edge/Chrome,
reused from the system — no bundled browser) loads the video page with the
saved cookies, signs the request, and exposes the real CDN stream URL. The
small ``tools/douyin_extract.mjs`` script drives the browser over CDP with zero
npm dependencies (Node's built-in WebSocket); we then download the MP4 here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from backend.app.services.cookie_vault import CookieVault
from backend.app.studio.providers import extract_url

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_BROWSER_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/microsoft-edge",
]


class BrowserExtractError(RuntimeError):
    pass


def find_browser(explicit: str | None = None) -> str | None:
    for candidate in [explicit, *_BROWSER_CANDIDATES]:
        if candidate and Path(candidate).exists():
            return candidate
    return None


class DouyinBrowserExtractor:
    def __init__(self, videos_dir: Path, cookie_vault: CookieVault, root_dir: Path, browser_path: str | None = None, node_bin: str = "node"):
        self.videos_dir = videos_dir
        self.cookie_vault = cookie_vault
        self.script = root_dir / "tools" / "douyin_extract.mjs"
        self.posts_script = root_dir / "tools" / "douyin_user_posts.mjs"
        self.browser_path = find_browser(browser_path)
        self.node_bin = node_bin

    def available(self) -> bool:
        return bool(self.browser_path) and shutil.which(self.node_bin) is not None and self.script.exists()

    def list_user_posts(self, user_url: str, limit: int = 200, timeout_ms: int = 105000) -> list[dict]:
        """List a creator's posts via the headless browser.

        Loads the /user/<sec_uid> page (Douyin's JS signs the request), scrolls to
        lazy-load, captures the aweme/post API responses, and returns
        ``[{id, desc, duration, cover}, ...]``. The lister scrolls until the channel
        runs out (has_more=false) or ``limit``/``timeout_ms`` is hit — so a high limit +
        generous timeout walks the WHOLE channel, not just the newest page.
        """
        if not self.browser_path:
            raise BrowserExtractError("Không tìm thấy Chrome/Edge để chạy headless. Đặt AUTOREUP_BROWSER_PATH.")
        if not self.posts_script.exists():
            raise BrowserExtractError("Thiếu script liệt kê kênh (douyin_user_posts.mjs).")
        cookies = self.cookie_vault.cookie_objects("douyin")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(cookies, handle)
            cookies_path = handle.name
        try:
            result = subprocess.run(
                [self.node_bin, str(self.posts_script), "--url", user_url, "--cookies", cookies_path,
                 "--browser", self.browser_path, "--limit", str(limit), "--timeout", str(timeout_ms)],
                capture_output=True, text=True, timeout=timeout_ms / 1000 + 20,
            )
        finally:
            Path(cookies_path).unlink(missing_ok=True)
        if result.returncode != 0 or not result.stdout.strip():
            raise BrowserExtractError(_reason(result.stderr) or "Không liệt kê được video của kênh.")
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BrowserExtractError("Kết quả liệt kê kênh không hợp lệ.") from exc
        return info.get("videos") or []

    def download(self, url: str, video_id: str) -> dict:
        if not shutil.which(self.node_bin):
            raise BrowserExtractError("Node.js không có sẵn (cần cho bộ tải Douyin bằng trình duyệt).")
        if not self.browser_path:
            raise BrowserExtractError("Không tìm thấy Chrome/Edge để chạy headless. Đặt AUTOREUP_BROWSER_PATH.")

        page_url = self._page_url(url)
        info = self._resolve_stream(page_url)
        dest = self.videos_dir / f"{video_id}.mp4"
        self._download_and_mux(info, dest, video_id)
        return {
            "id": video_id,
            "title": info.get("title") or video_id,
            "duration": info.get("duration"),
            "thumbnail_url": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "path": dest,
            "webpage_url": page_url,
        }

    def _page_url(self, url: str) -> str:
        url = extract_url(url)
        from backend.app.studio.providers import _extract_video_id, SourceDiscovery  # local import to avoid cycle

        resolved = SourceDiscovery._resolve_redirect(url) or url
        video_id = _extract_video_id(resolved) or _extract_video_id(url)
        return f"https://www.douyin.com/video/{video_id}" if video_id else resolved

    def _resolve_stream(self, page_url: str) -> dict:
        cookies = self.cookie_vault.cookie_objects("douyin")
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(cookies, handle)
            cookies_path = handle.name
        try:
            result = subprocess.run(
                [self.node_bin, str(self.script), "--url", page_url, "--cookies", cookies_path,
                 "--browser", self.browser_path, "--timeout", "55000"],
                capture_output=True,
                text=True,
                # Backstop only: the script has its own 78s watchdog and fails fast on a
                # CAPTCHA page, so this should rarely fire.
                timeout=95,
            )
        finally:
            Path(cookies_path).unlink(missing_ok=True)
        if result.returncode != 0 or not result.stdout.strip():
            raise BrowserExtractError(_reason(result.stderr) or "Trình duyệt headless không lấy được link video.")
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BrowserExtractError("Kết quả trích xuất không hợp lệ.") from exc
        if not info.get("video"):
            raise BrowserExtractError("Không tìm thấy luồng video.")
        return info

    def _download_and_mux(self, info: dict, dest: Path, video_id: str) -> None:
        # Douyin serves separate adaptive tracks; download both and mux to one MP4.
        dest.parent.mkdir(parents=True, exist_ok=True)
        video_path = self.videos_dir / f"{video_id}.v.mp4"
        self._download_stream(info["video"], video_path)
        if info.get("audio"):
            audio_path = self.videos_dir / f"{video_id}.a.mp4"
            self._download_stream(info["audio"], audio_path)
            self._mux(video_path, audio_path, dest)
            video_path.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)
        else:
            video_path.replace(dest)

    def _download_stream(self, stream_url: str, dest: Path) -> None:
        cookie_header = self.cookie_vault.read_header("douyin") or ""
        headers = {"User-Agent": _UA, "Referer": "https://www.douyin.com/", "Cookie": cookie_header}
        with httpx.stream("GET", stream_url, headers=headers, follow_redirects=True, timeout=120) as response:
            if response.status_code != 200:
                raise BrowserExtractError(f"Tải luồng lỗi HTTP {response.status_code}.")
            with dest.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        if dest.stat().st_size < 1024:
            raise BrowserExtractError("File tải về rỗng/không hợp lệ.")

    def _mux(self, video_path: Path, audio_path: Path, dest: Path) -> None:
        if not shutil.which("ffmpeg"):
            raise BrowserExtractError("Cần ffmpeg để ghép video+audio.")
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(video_path), "-i", str(audio_path),
             "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(dest)],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0 or not dest.exists():
            raise BrowserExtractError(f"Ghép video+audio lỗi: {result.stderr.strip()[:160]}")


def _reason(stderr: str) -> str:
    text = (stderr or "").strip().splitlines()
    return text[-1][:160] if text else ""
