"""Douyin download + channel listing via the signed web API (no browser).

Drop-in replacement for :class:`DouyinBrowserExtractor` (same ``download`` /
``list_user_posts`` / ``available`` surface), so callers try this first and fall
back to the browser when the API is blocked (empty risk-control response).
"""

from __future__ import annotations

from pathlib import Path

import httpx

from backend.app.services.cookie_vault import CookieVault
from backend.app.studio.providers import _extract_sec_uid, _extract_video_id, extract_url

from .api_client import DouyinApiClient, DouyinApiError

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


class DouyinApiExtractor:
    def __init__(self, videos_dir: Path, cookie_vault: CookieVault):
        self.videos_dir = videos_dir
        self.cookie_vault = cookie_vault

    def available(self) -> bool:
        return True  # pure Python + cookie; no browser/node needed

    def _client(self) -> DouyinApiClient:
        return DouyinApiClient(self.cookie_vault.read_header("douyin") or "")

    def list_user_posts(self, user_url: str, limit: int = 200, timeout_ms: int = 105000) -> list[dict]:
        sec_uid = _extract_sec_uid(user_url)
        if not sec_uid:
            raise DouyinApiError("Không đọc được sec_user_id từ link kênh.")
        return self._client().user_posts(sec_uid, limit=limit)

    def download(self, url: str, video_id: str) -> dict:
        aweme_id = _extract_video_id(extract_url(url))
        if not aweme_id:
            raise DouyinApiError("Không đọc được id video Douyin từ link.")
        stream = self._client().best_stream(aweme_id)
        dest = self.videos_dir / f"{video_id}.mp4"
        self._download(stream["url"], dest)
        return {
            "id": video_id,
            "title": stream.get("title") or video_id,
            "duration": stream.get("duration"),
            "thumbnail_url": stream.get("thumbnail"),
            "uploader": stream.get("uploader"),
            "path": dest,
            "webpage_url": f"https://www.douyin.com/video/{aweme_id}",
        }

    def _download(self, stream_url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": _UA, "Referer": "https://www.douyin.com/", "Cookie": self.cookie_vault.read_header("douyin") or ""}
        with httpx.stream("GET", stream_url, headers=headers, follow_redirects=True, timeout=180) as resp:
            if resp.status_code != 200:
                raise DouyinApiError(f"Tải luồng lỗi HTTP {resp.status_code}.")
            with dest.open("wb") as out:
                for chunk in resp.iter_bytes():
                    out.write(chunk)
        if not dest.exists() or dest.stat().st_size < 1024:
            raise DouyinApiError("File tải về rỗng/không hợp lệ.")
