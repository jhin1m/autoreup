import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class ProviderSpec:
    platform: str
    label: str
    required: tuple[str, ...]
    recommended: tuple[str, ...]
    note: str


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "douyin": ProviderSpec(
        platform="douyin",
        label="Douyin",
        required=("ttwid",),
        recommended=("s_v_web_id", "msToken"),
        note="Dùng cho scan public best-effort và metadata/download khi web yêu cầu phiên đăng nhập.",
    ),
    "bilibili": ProviderSpec(
        platform="bilibili",
        label="Bilibili",
        required=("SESSDATA",),
        recommended=("bili_jct", "DedeUserID"),
        note="Giúp giảm lỗi risk-control khi quét space/list và dùng cho nội dung cần đăng nhập.",
    ),
}


class CookieVault:
    def __init__(self, cookies_dir: Path):
        self.cookies_dir = cookies_dir
        self.cookies_dir.mkdir(parents=True, exist_ok=True)

    def status_all(self) -> list[dict]:
        return [self.status(platform) for platform in PROVIDER_SPECS]

    def status(self, platform: str) -> dict:
        spec = self._spec(platform)
        path = self._path(platform)
        cookie_names = self.cookie_names(platform) if path.exists() else []
        missing = [name for name in spec.required if name not in cookie_names]
        updated_at = None
        if path.exists():
            updated_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        return {
            "platform": spec.platform,
            "label": spec.label,
            "configured": path.exists() and bool(cookie_names),
            "updated_at": updated_at,
            "cookie_count": len(cookie_names),
            "cookie_names": cookie_names,
            "required_missing": missing,
            "recommended_missing": [name for name in spec.recommended if name not in cookie_names],
            "note": spec.note,
        }

    def save(self, platform: str, cookie_header: str) -> dict:
        self._spec(platform)
        normalized = self._normalize_cookie_header(cookie_header)
        if not normalized:
            raise ValueError("Cookie không hợp lệ.")
        path = self._path(platform)
        path.write_text(normalized, encoding="utf-8")
        os.chmod(path, 0o600)
        return self.status(platform)

    def clear(self, platform: str) -> dict:
        self._spec(platform)
        path = self._path(platform)
        if path.exists():
            path.unlink()
        return self.status(platform)

    def read_header(self, platform: str) -> str | None:
        self._spec(platform)
        path = self._path(platform)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def cookie_names(self, platform: str) -> list[str]:
        header = self.read_header(platform)
        if not header:
            return []
        names = []
        for part in header.split(";"):
            name = part.strip().split("=", 1)[0].strip()
            if name and name not in names:
                names.append(name)
        return names

    def cookie_file(self, platform: str) -> Path | None:
        self._spec(platform)
        path = self._path(platform)
        return path if path.exists() else None

    def _path(self, platform: str) -> Path:
        return self.cookies_dir / f"{platform}.cookie"

    @staticmethod
    def _normalize_cookie_header(cookie_header: str) -> str:
        lines = [line.strip() for line in cookie_header.strip().splitlines() if line.strip()]
        if not lines:
            return ""
        header = "; ".join(lines)
        return "; ".join(part.strip() for part in header.split(";") if "=" in part)

    @staticmethod
    def _spec(platform: str) -> ProviderSpec:
        try:
            return PROVIDER_SPECS[platform]
        except KeyError as exc:
            raise ValueError(f"Unsupported provider: {platform}") from exc
