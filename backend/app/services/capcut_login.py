"""Native CapCut email/password login — pure-Python port of the kuwacom login flow,
so AutoReup can mint and refresh its own CapCut session (no node service at all).

All the crypto is small and deterministic:
  * xor5-hex:        each utf8 byte -> (byte ^ 5) as 2 hex digits
  * sensitive body:  email/password xor5-hex encoded, with mix_mode=fixed_mix_mode=1
  * verifyFp / tdid: client-generated ids (CapCut accepts whatever we send)

Flow: GET the login page (primes csrf + _tea_web_id cookies) -> POST
/passport/web/email/login/ with the sensitive body + the csrf header. Cookies are
managed by http.cookiejar. We persist {cookies, deviceId, tdid} as the session file
the synth client reads.
"""

from __future__ import annotations

import http.cookiejar
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_LOGIN_SDK_VERSION = "2.1.10-tiktok"
_EMAIL_HASH_SALT = "aDy0TUhtql92P7hScCs97YWMT-jub2q9"


class CapCutLoginError(RuntimeError):
    pass


@dataclass(slots=True)
class LoginConfig:
    email: str
    password: str
    session_file: Path
    app_id: str = "348188"
    web_url: str = "https://www.capcut.com"
    login_host: str = "https://login-row.www.capcut.com"
    fallback_login_host: str = "https://login.us.capcut.com"
    page_locale: str = "vi-vn"
    locale: str = "vi-VN"
    store_country: str = "vn"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    email_login_path: str = "/passport/web/email/login/"
    timeout: float = 30.0


def _xor5_hex(value: str) -> str:
    return "".join(f"{b ^ 5:02x}" for b in value.encode("utf-8"))


def _sensitive_body(values: dict[str, str], sensitive: list[str]) -> str:
    payload: dict[str, str] = {}
    for key, value in values.items():
        payload[key] = _xor5_hex(value) if key in sensitive else value
    mix = 1 if any(k in sensitive for k in values) else 0
    payload["mix_mode"] = str(mix)
    payload["fixed_mix_mode"] = str(mix)
    return urllib.parse.urlencode(payload)


def _create_verify_fp() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    rand = lambda n: "".join(secrets.choice(alphabet) for _ in range(n))  # noqa: E731
    prefix = format(int(time.time() * 1000) % (36**8), "08x")
    return f"verify_{prefix}_{rand(8)}_{rand(4)}_{rand(4)}_{rand(4)}_{rand(12)}"


def _create_tracking_id() -> str:
    return (str(int(time.time() * 1000)) + "".join(str(secrets.randbelow(10)) for _ in range(8)))[:17]


def _cookie(jar: http.cookiejar.CookieJar, name: str) -> str | None:
    for cookie in jar:
        if cookie.name == name and cookie.value:
            return cookie.value
    return None


def login(config: LoginConfig) -> dict:
    """Log in and return the session dict; also writes it to ``config.session_file``."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    verify_fp = _create_verify_fp()
    tdid = _create_tracking_id()

    # 1) prime: load the login page so CapCut sets passport_csrf_token + _tea_web_id
    page = urllib.request.Request(
        f"{config.web_url}/{config.page_locale}/login",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": config.locale,
            "User-Agent": config.user_agent,
        },
    )
    try:
        opener.open(page, timeout=config.timeout).read()
    except urllib.error.URLError as exc:
        raise CapCutLoginError(f"login page prime failed: {exc}") from exc

    device_id = _cookie(jar, "_tea_web_id") or _cookie(jar, "web_id") or _cookie(jar, "did") or tdid
    csrf = _cookie(jar, "passport_csrf_token") or _cookie(jar, "passport_csrf_token_default") or ""

    # 2) POST email/login
    params = urllib.parse.urlencode({
        "aid": config.app_id,
        "account_sdk_source": "web",
        "sdk_version": _LOGIN_SDK_VERSION,
        "language": config.locale,
        "verifyFp": verify_fp,
    })
    body = _sensitive_body({"email": config.email, "password": config.password}, ["email", "password"]).encode("utf-8")
    headers = {
        "Accept": "application/json, text/javascript",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": config.user_agent,
        "appid": config.app_id,
        "did": device_id,
        "Origin": config.web_url,
        "Referer": f"{config.web_url}/{config.page_locale}/login",
        "store-country-code": config.store_country,
        "store-country-code-src": "uid",
        "x-tt-passport-csrf-token": csrf,
    }

    last_error: str | None = None
    for host in (config.login_host, config.fallback_login_host):
        request = urllib.request.Request(f"{host}{config.email_login_path}?{params}", data=body, headers=headers, method="POST")
        try:
            raw = opener.open(request, timeout=config.timeout).read()
        except urllib.error.URLError as exc:
            last_error = str(exc)
            continue
        data = json.loads(raw)
        if str(data.get("message")).lower() == "success":
            device_id = _cookie(jar, "_tea_web_id") or device_id
            session = {
                "deviceId": device_id,
                "tdid": tdid,
                "cookies": [
                    {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": bool(c.secure)}
                    for c in jar
                    if c.value
                ],
                "loggedInAt": int(time.time() * 1000),
            }
            Path(config.session_file).parent.mkdir(parents=True, exist_ok=True)
            Path(config.session_file).write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
            return session
        err = (data.get("data") or {}).get("description") or data.get("message")
        last_error = f"ret={data.get('data', {}).get('error_code')} {err!r}"
    raise CapCutLoginError(f"CapCut email login failed: {last_error}")
