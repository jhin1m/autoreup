import json
import threading
from pathlib import Path

from backend.app.studio.models import DownloadQueueItem, SourceVideo, StudioState, VideoAsset


class StudioStore:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self._lock = threading.Lock()

    def load(self) -> StudioState:
        if not self.state_path.exists():
            return StudioState()
        try:
            return StudioState.model_validate_json(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return StudioState()

    def save(self, state: StudioState) -> None:
        with self._lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
            tmp.replace(self.state_path)

    def upsert_source_videos(self, videos: list[SourceVideo]) -> StudioState:
        state = self.load()
        by_id = {video.id: video for video in state.source_videos}
        for video in videos:
            existing = by_id.get(video.id)
            if existing:
                video.download_state = existing.download_state
            by_id[video.id] = video
        state.source_videos = sorted(by_id.values(), key=lambda item: item.discovered_at, reverse=True)
        self.save(state)
        return state

    def source_video(self, video_id: str) -> SourceVideo | None:
        return next((video for video in self.load().source_videos if video.id == video_id), None)

    def queue_items(self, items: list[DownloadQueueItem]) -> StudioState:
        state = self.load()
        queued_ids = {item.source_video_id for item in state.download_queue if item.state in {"queued", "running"}}
        existing = {item.id: item for item in state.download_queue}
        for item in items:
            if item.source_video_id not in queued_ids:
                existing[item.id] = item
        for video in state.source_videos:
            if video.id in {item.source_video_id for item in items} and video.download_state == "not_queued":
                video.download_state = "queued"
        state.download_queue = sorted(existing.values(), key=lambda item: item.created_at, reverse=True)
        self.save(state)
        return state

    def update_queue_item(self, item_id: str, **changes) -> StudioState:
        state = self.load()
        for item in state.download_queue:
            if item.id == item_id:
                for key, value in changes.items():
                    setattr(item, key, value)
                break
        source_video_id = changes.get("source_video_id")
        state_value = changes.get("state")
        if source_video_id and state_value:
            for video in state.source_videos:
                if video.id == source_video_id:
                    video.download_state = state_value
        self.save(state)
        return state

    def add_asset(self, asset: VideoAsset) -> StudioState:
        state = self.load()
        state.assets = [asset, *[item for item in state.assets if item.id != asset.id]]
        for video in state.source_videos:
            if video.id == asset.source_video_id:
                video.download_state = "downloaded"
        for item in state.download_queue:
            if item.source_video_id == asset.source_video_id:
                item.state = "downloaded"
                item.error = None
        self.save(state)
        return state
