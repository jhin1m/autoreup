"""Signed Douyin web-API client — no headless browser.

Generates the ``a_bogus`` signature in-process (:mod:`.abogus`) and calls the
web endpoints directly with the saved cookie: same responses the browser
sniffed (identical video quality), just faster and browser-free. The browser
extractor stays as a fallback.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlencode, quote

import httpx

from .abogus import ABogus

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_DETAIL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_POST = "https://www.douyin.com/aweme/v1/web/aweme/post/"
_MIX = "https://www.douyin.com/aweme/v1/web/mix/aweme/"

_BASE_PARAMS = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "pc_client_type": "1", "version_code": "190500", "version_name": "19.5.0",
    "cookie_enabled": "true", "browser_language": "zh-CN", "browser_platform": "Win32",
    "browser_name": "Chrome", "browser_online": "true", "engine_name": "Blink",
    "os_name": "Windows", "os_version": "10", "platform": "PC",
    "screen_width": "1920", "screen_height": "1080", "browser_version": "124.0.0.0",
    "engine_version": "124.0.0.0", "cpu_core_num": "12", "device_memory": "8",
}


class DouyinApiError(RuntimeError):
    pass


class DouyinDrmError(DouyinApiError):
    """The stream downloaded but its samples are encrypted (playlet/短剧 DRM) — the
    web API can't yield a playable file; the app can. Not worth a browser fallback."""


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class Gear:
    width: int
    height: int
    bit_rate: int
    is_h265: bool
    gear_name: str
    url: str

    @property
    def effective_bitrate(self) -> float:
        # H.265 ≈ 1.7x quality per bit vs H.264.
        return self.bit_rate * (1.7 if self.is_h265 else 1.0)


def pick_best_gear(bit_rate_list: list[dict]) -> Gear | None:
    best: Gear | None = None
    for g in bit_rate_list or []:
        pa = g.get("play_addr") or {}
        urls = pa.get("url_list") or []
        if not urls:
            continue
        gear = Gear(
            width=pa.get("width") or 0, height=pa.get("height") or 0,
            bit_rate=g.get("bit_rate") or 0, is_h265=(g.get("is_h265") == 1),
            gear_name=g.get("gear_name") or "", url=urls[0],
        )
        if best is None or (gear.width * gear.height, gear.effective_bitrate) > (best.width * best.height, best.effective_bitrate):
            best = gear
    return best


class DouyinApiClient:
    def __init__(self, cookie_header: str, timeout: float = 25.0, attempts: int = 4):
        self.cookie_header = cookie_header or ""
        self.timeout = timeout
        self.attempts = attempts
        self._abogus = ABogus()

    def _signed_url(self, endpoint: str, params: dict) -> str:
        qs = urlencode({**_BASE_PARAMS, **params})
        a_bogus = quote(self._abogus.get_value(qs, "GET"), safe="")
        return f"{endpoint}?{qs}&a_bogus={a_bogus}"

    def _get(self, endpoint: str, params: dict) -> dict:
        headers = {
            "User-Agent": _UA, "Referer": "https://www.douyin.com/",
            "Accept": "application/json, text/plain, */*", "Cookie": self.cookie_header,
        }
        # Douyin returns an empty body under transient risk-control. Retry with a fresh
        # signature and backoff before giving up (the caller then falls back to browser).
        last = ""
        for i in range(max(1, self.attempts)):
            try:
                resp = httpx.get(self._signed_url(endpoint, params), headers=headers, timeout=self.timeout, follow_redirects=True)
            except httpx.HTTPError as exc:
                last = f"Kết nối Douyin lỗi: {exc}"
            else:
                if resp.status_code == 200 and resp.content:
                    try:
                        return resp.json()
                    except ValueError:
                        last = "Douyin trả dữ liệu không phải JSON."
                elif not resp.content:
                    last = "Douyin chặn (rỗng) — cookie hết hạn hoặc cần xác minh."
                else:
                    last = f"Douyin trả HTTP {resp.status_code}"
            if i + 1 < self.attempts:
                time.sleep(0.8 * (i + 1))
        raise DouyinApiError(last or "Douyin không phản hồi.")

    def detail(self, aweme_id: str) -> dict:
        data = self._get(_DETAIL, {"aweme_id": str(aweme_id)})
        aweme = data.get("aweme_detail")
        if not aweme:
            raise DouyinApiError("Douyin không trả chi tiết video (có thể bị khoá/riêng tư).")
        return aweme

    def best_stream(self, aweme_id: str) -> dict:
        aweme = self.detail(aweme_id)
        video = aweme.get("video") or {}
        gear = pick_best_gear(video.get("bit_rate") or [])
        if gear is None:
            raise DouyinApiError("Không tìm thấy luồng tải cho video này.")
        cover = (video.get("cover") or {}).get("url_list") or []
        author = aweme.get("author") or {}
        return {
            "url": gear.url,
            "width": gear.width, "height": gear.height,
            "is_h265": gear.is_h265, "gear": gear.gear_name,
            "title": (aweme.get("desc") or "").strip() or str(aweme_id),
            "duration": (video.get("duration") or aweme.get("duration") or 0) / 1000 or None,
            "thumbnail": cover[0] if cover else None,
            "uploader": author.get("nickname"),
        }

    def mix_episodes(self, mix_id: str, limit: int = 300, page: int = 20) -> tuple[str, list[dict]]:
        """Episodes of a 合集/playlet (短剧) → ``(mix_name, [{id, desc, duration, cover,
        episode, paid}, ...])`` in episode order. ``paid`` flags a locked episode."""
        out: list[dict] = []
        name = ""
        cursor = 0
        while len(out) < limit:
            data = self._get(_MIX, {"mix_id": str(mix_id), "cursor": str(cursor), "count": str(page)})
            for aweme in data.get("aweme_list") or []:
                aid = str(aweme.get("aweme_id") or "")
                if not aid:
                    continue
                video = aweme.get("video") or {}
                cover = (video.get("cover") or {}).get("url_list") or []
                mix = aweme.get("mix_info") or {}
                name = name or (mix.get("mix_name") or "").strip()
                paid_status = (aweme.get("series_paid_info") or {}).get("series_paid_status", 0)
                out.append({
                    "id": aid,
                    "desc": (aweme.get("desc") or "").strip(),
                    "duration": (video.get("duration") or 0) / 1000 or None,
                    "cover": cover[0] if cover else None,
                    "episode": _int_or_none((mix.get("statis") or {}).get("current_episode")),
                    "paid": bool(paid_status) or not (video.get("bit_rate")),
                })
            cursor = data.get("cursor") or 0
            if not data.get("has_more") or not cursor:
                break
        out.sort(key=lambda e: e.get("episode") or 0)
        return name, out[:limit]

    def user_posts(self, sec_user_id: str, limit: int = 200, page: int = 18) -> list[dict]:
        out: list[dict] = []
        cursor = 0
        while len(out) < limit:
            data = self._get(_POST, {"sec_user_id": sec_user_id, "max_cursor": str(cursor), "count": str(page)})
            for aweme in data.get("aweme_list") or []:
                aid = str(aweme.get("aweme_id") or "")
                if not aid:
                    continue
                video = aweme.get("video") or {}
                cover = (video.get("cover") or {}).get("url_list") or []
                out.append({
                    "id": aid,
                    "desc": (aweme.get("desc") or "").strip(),
                    "duration": (video.get("duration") or 0) / 1000 or None,
                    "cover": cover[0] if cover else None,
                })
            cursor = data.get("max_cursor") or 0
            if not data.get("has_more") or not cursor:
                break
        return out[:limit]
