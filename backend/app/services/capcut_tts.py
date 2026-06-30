"""Native (in-process) CapCut TTS client — a pure-Python port of the kuwacom
CapCut-TTS node wrapper, so AutoReup talks to CapCut directly with no external
service.

The only non-trivial part is the request signature, which is just an MD5 of a
pipe-joined string (no obfuscated JS):

    raw  = f"{prefix}|{path[-tail:]}|{platform_id}|{app_version}|{device_time}|{tdid}|{suffix}"
    sign = md5(raw).hexdigest()           # headers: sign, sign-ver

Upstream: POST {edit_api}/storyboard/v1/tts/multi_platform with a ``texts`` array
(true batch, up to 100) and ``need_url=true``; the response carries
``data.tts_materials[i].meta_data.url`` (one audio per text), which we download.

Auth is a persisted CapCut session (cookies/deviceId/tdid) loaded from
``capcut-session.json``. Session refresh (login) is out of scope here — keep the
session file fresh; an expired session surfaces as an auth error from CapCut.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_MULTI_PLATFORM_PATH = "/storyboard/v1/tts/multi_platform"


class CapCutTtsError(RuntimeError):
    pass


@dataclass(slots=True)
class CapCutConfig:
    session_file: Path
    edit_api: str = "https://edit-api-sg.capcut.com"
    web_url: str = "https://www.capcut.com"
    app_id: str = "348188"
    app_version: str = "5.8.0"
    platform_id: str = "7"
    sign_version: str = "1"
    sign_prefix: str = "9e2c"
    sign_suffix: str = "11ac"
    sign_path_tail: int = 7
    store_country: str = "vn"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    speaker: str = "BV421_vivn_streaming"
    effect_id: str = "7252594014782755330"
    resource_id: str = "7252594014782755330"
    voice_name: str = "Nguồn nhỏ ngọt ngào"
    timeout: float = 120.0
    attempts: int = 3


class CapCutTtsClient:
    def __init__(self, config: CapCutConfig):
        self.config = config
        self._load_session()

    # --- session -----------------------------------------------------------
    def _load_session(self) -> None:
        data = json.loads(Path(self.config.session_file).read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        self._device_id: str = str(data.get("deviceId") or data.get("session", {}).get("deviceId") or "")
        self._tdid: str = str(data.get("tdid") or "")
        self._cookie_header: str = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies if "capcut.com" in (c.get("domain") or "")
        )
        if not self._cookie_header or not self._device_id:
            raise CapCutTtsError("CapCut session file missing cookies/deviceId")

    # --- signing -----------------------------------------------------------
    def _sign(self) -> tuple[str, str]:
        device_time = str(int(time.time()))
        cfg = self.config
        tail = _MULTI_PLATFORM_PATH[-cfg.sign_path_tail :]
        raw = f"{cfg.sign_prefix}|{tail}|{cfg.platform_id}|{cfg.app_version}|{device_time}|{self._tdid}|{cfg.sign_suffix}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest().lower(), device_time

    def _headers(self) -> dict[str, str]:
        sign, device_time = self._sign()
        cfg = self.config
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": cfg.web_url,
            "Referer": f"{cfg.web_url}/",
            "User-Agent": cfg.user_agent,
            "appid": cfg.app_id,
            "appvr": cfg.app_version,
            "device-time": device_time,
            "did": self._device_id,
            "pf": cfg.platform_id,
            "sign": sign,
            "sign-ver": cfg.sign_version,
            "store-country-code": cfg.store_country,
            "store-country-code-src": "uid",
            "tdid": self._tdid,
            "Cookie": self._cookie_header,
        }

    # --- synthesis ---------------------------------------------------------
    def _post_multi_platform(self, texts: list[str], rate: float, volume: float) -> list[str]:
        cfg = self.config
        body = json.dumps(
            {
                "texts": texts,
                "tts_conf": {
                    "speaker": cfg.speaker,
                    "rate": round(min(max(rate, 0.5), 2.0), 2),
                    "volume": int(min(max(volume, 0.0), 20.0) * 10),
                    "name": cfg.voice_name,
                    "platform": "sami",
                    "effect_id": cfg.effect_id,
                    "resource_id": cfg.resource_id,
                    "is_clone": False,
                },
                "need_url": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(1, cfg.attempts + 1):
            try:
                request = urllib.request.Request(
                    cfg.edit_api + _MULTI_PLATFORM_PATH, data=body, headers=self._headers(), method="POST"
                )
                with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
                    payload = json.loads(response.read())
                ret = str(payload.get("ret"))
                if ret != "0":
                    raise CapCutTtsError(f"CapCut TTS ret={ret} {payload.get('errmsg')!r}")
                materials = (payload.get("data") or {}).get("tts_materials") or payload.get("tts_materials") or []
                if len(materials) < len(texts):
                    raise CapCutTtsError(f"CapCut returned {len(materials)} materials for {len(texts)} texts")
                urls: list[str] = []
                for index in range(len(texts)):
                    material = materials[index]
                    url = (material.get("meta_data") or {}).get("url") or (material.get("extra_map") or {}).get("url_office")
                    if not url:
                        raise CapCutTtsError(f"CapCut material {index} missing url")
                    urls.append(url)
                return urls
            except (urllib.error.URLError, TimeoutError, CapCutTtsError, ValueError) as exc:
                last_error = exc
                if attempt < cfg.attempts:
                    time.sleep(0.6 * (attempt + 1))
        raise CapCutTtsError(f"CapCut multi_platform failed after {cfg.attempts} attempts: {last_error}")

    def _download(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": self.config.user_agent, "Accept": "*/*"})
        with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
            audio = response.read()
        if not audio:
            raise CapCutTtsError("CapCut audio download was empty")
        return audio

    def synthesize_batch(self, texts: list[str], rate: float = 1.0, volume: float = 10.0) -> list[bytes]:
        """One signed CapCut call for up to ~100 texts, then download each audio.
        ``rate`` is the CapCut playback rate (>1.0 = faster/snappier delivery)."""
        if not texts:
            return []
        urls = self._post_multi_platform(texts, rate, volume)
        with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
            return list(pool.map(self._download, urls))

    def synthesize(self, text: str, rate: float = 1.0, volume: float = 10.0) -> bytes:
        return self.synthesize_batch([text], rate=rate, volume=volume)[0]


def synthesize_many(
    client: CapCutTtsClient,
    texts: list[str],
    batch_size: int,
    concurrency: int,
    rate: float = 1.0,
    volume: float = 10.0,
) -> list[bytes]:
    """Synthesize many texts as CONCURRENT batches (the fast path): split into
    batches of ``batch_size`` and run ``concurrency`` of them in parallel.
    Returns audio bytes in the same order as ``texts``."""
    if not texts:
        return []
    batches = [texts[start : start + batch_size] for start in range(0, len(texts), max(1, batch_size))]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(lambda batch: client.synthesize_batch(batch, rate=rate, volume=volume), batches))
    out: list[bytes] = []
    for chunk in results:
        out.extend(chunk)
    return out
