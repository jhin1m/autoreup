import threading
from uuid import uuid4

from backend.app.core.config import Settings
from backend.app.services.cookie_vault import CookieVault
from backend.app.studio.models import DownloadQueueItem, DownloadState, VideoAsset, now_iso
from backend.app.studio.providers import SourceDiscovery
from backend.app.studio.store import StudioStore


class StudioService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = StudioStore(settings.data_dir / "studio" / "state.json")
        self.cookie_vault = CookieVault(settings.provider_cookies_dir)
        self.discovery = SourceDiscovery(self.cookie_vault, settings)

    def state(self):
        return self.store.load()

    def discover(self, url: str, limit: int):
        videos = self.discovery.discover(url, limit)
        self.store.upsert_source_videos(videos)
        return videos

    def enqueue_downloads(self, video_ids: list[str]):
        items = []
        for video_id in video_ids:
            video = self.store.source_video(video_id)
            if not video:
                continue
            items.append(DownloadQueueItem(id=uuid4().hex[:12], source_video_id=video.id))
        state = self.store.queue_items(items)
        for item in items:
            threading.Thread(target=self._download_item, args=(item.id,), daemon=True).start()
        return state

    def _download_item(self, item_id: str) -> None:
        state = self.store.load()
        item = next((entry for entry in state.download_queue if entry.id == item_id), None)
        if not item:
            return
        video = next((entry for entry in state.source_videos if entry.id == item.source_video_id), None)
        if not video:
            return
        self.store.update_queue_item(item.id, state=DownloadState.running, updated_at=now_iso(), source_video_id=video.id)
        try:
            from backend.app.pipeline.production import _download_source

            result = _download_source(self.settings, video.source_url, video.id)
            asset = VideoAsset(
                id=video.id,
                source_video_id=video.id,
                platform=video.platform,
                title=result.get("title") or video.title,
                source_url=result.get("webpage_url") or video.source_url,
                thumbnail_url=result.get("thumbnail_url") or video.thumbnail_url,
                duration=result.get("duration") or video.duration,
                local_video_path=str(result["path"].relative_to(self.settings.data_dir)),
            )
            self.store.add_asset(asset)
        except Exception as exc:
            self.store.update_queue_item(
                item.id,
                state=DownloadState.failed,
                updated_at=now_iso(),
                source_video_id=video.id,
                error=str(exc),
            )
