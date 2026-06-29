import hashlib
import json
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from backend.app.services.cookie_vault import CookieVault
from backend.app.studio.models import Platform, SourceVideo


def detect_platform(url: str) -> Platform:
    host = urlparse(url).netloc.lower()
    if "douyin.com" in host:
        return Platform.douyin
    if "bilibili.com" in host or url.startswith("BV"):
        return Platform.bilibili
    if "youtube.com" in host or "youtu.be" in host:
        return Platform.youtube
    return Platform.unknown


class SourceDiscovery:
    def __init__(self, cookie_vault: CookieVault):
        self.cookie_vault = cookie_vault

    def discover(self, url: str, limit: int = 12) -> list[SourceVideo]:
        platform = detect_platform(url)
        if platform == Platform.bilibili and self._is_bilibili_space(url):
            return self._discover_bilibili_space(url, limit)
        return [self._discover_single(url, platform)]

    def _discover_single(self, url: str, platform: Platform) -> SourceVideo:
        if platform == Platform.bilibili:
            return self._discover_bilibili_video(url)
        info = self._extract_info(url, flat=False, platform=platform)
        return self._video_from_info(info, url, platform)

    def _discover_bilibili_video(self, url: str) -> SourceVideo:
        bvid = _extract_video_id(url) or url.strip()
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        req = Request(api_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
        with urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode())
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("message") or "Bilibili metadata failed")
        data = payload.get("data") or {}
        owner = data.get("owner") or {}
        return SourceVideo(
            id=self._id_for(Platform.bilibili, data.get("bvid") or bvid),
            platform=Platform.bilibili,
            platform_video_id=data.get("bvid") or bvid,
            source_url=f"https://www.bilibili.com/video/{data.get('bvid') or bvid}",
            title=data.get("title") or bvid,
            thumbnail_url=data.get("pic"),
            duration=data.get("duration"),
            uploader=owner.get("name"),
        )

    def _discover_bilibili_space(self, url: str, limit: int) -> list[SourceVideo]:
        playlist = self._extract_info(url, flat=True, platform=Platform.bilibili, playlist_end=limit)
        videos = []
        for entry in (playlist.get("entries") or [])[:limit]:
            bvid = entry.get("id") or entry.get("url")
            if not bvid:
                continue
            try:
                info = self._extract_info(f"https://www.bilibili.com/video/{bvid}", flat=False, platform=Platform.bilibili)
                videos.append(self._video_from_info(info, f"https://www.bilibili.com/video/{bvid}", Platform.bilibili))
            except Exception:
                videos.append(SourceVideo(
                    id=self._id_for(Platform.bilibili, bvid),
                    platform=Platform.bilibili,
                    platform_video_id=bvid,
                    source_url=f"https://www.bilibili.com/video/{bvid}",
                    title=entry.get("title") or bvid,
                    error="Không lấy được metadata chi tiết, vẫn có thể thử tải sau.",
                ))
        return videos

    def _extract_info(self, url: str, *, flat: bool, platform: Platform, playlist_end: int | None = None) -> dict:
        import yt_dlp

        headers = self._headers_for(platform)
        options: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist" if flat else False,
            "noplaylist": not flat,
            "http_headers": headers,
        }
        if playlist_end:
            options["playlistend"] = playlist_end
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)

    def _headers_for(self, platform: Platform) -> dict:
        headers = {"User-Agent": "Mozilla/5.0"}
        cookie = self.cookie_vault.read_header(platform.value) if platform in {Platform.douyin, Platform.bilibili} else None
        if cookie:
            headers["Cookie"] = cookie
        if platform == Platform.bilibili:
            headers["Referer"] = "https://www.bilibili.com/"
        if platform == Platform.douyin:
            headers["Referer"] = "https://www.douyin.com/"
        return headers

    @staticmethod
    def _video_from_info(info: dict, fallback_url: str, platform: Platform) -> SourceVideo:
        platform_id = str(info.get("id") or _extract_video_id(fallback_url) or fallback_url)
        thumbnails = info.get("thumbnails") or []
        thumbnail = info.get("thumbnail") or next((item.get("url") for item in reversed(thumbnails) if item.get("url")), None)
        return SourceVideo(
            id=SourceDiscovery._id_for(platform, platform_id),
            platform=platform,
            platform_video_id=platform_id,
            source_url=info.get("webpage_url") or fallback_url,
            title=info.get("title") or platform_id,
            thumbnail_url=thumbnail,
            duration=info.get("duration"),
            uploader=info.get("uploader") or info.get("channel"),
        )

    @staticmethod
    def _id_for(platform: Platform, platform_video_id: str) -> str:
        digest = hashlib.sha1(f"{platform}:{platform_video_id}".encode()).hexdigest()[:14]
        return f"{platform}-{digest}"

    @staticmethod
    def _is_bilibili_space(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.endswith("bilibili.com") and parsed.path.startswith("/") and parsed.path.split("/")[1:2] != ["video"] and "space.bilibili.com" in parsed.netloc


def _extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    if "douyin.com" in parsed.netloc and "/video/" in parsed.path:
        return parsed.path.rstrip("/").split("/")[-1]
    if "bilibili.com" in parsed.netloc and "/video/" in parsed.path:
        return parsed.path.rstrip("/").split("/")[-1]
    modal_id = parse_qs(parsed.query).get("modal_id", [None])[0]
    return modal_id
