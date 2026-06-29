from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Platform(StrEnum):
    douyin = "douyin"
    bilibili = "bilibili"
    youtube = "youtube"
    unknown = "unknown"


class DownloadState(StrEnum):
    not_queued = "not_queued"
    queued = "queued"
    running = "running"
    downloaded = "downloaded"
    failed = "failed"


class SourceVideo(BaseModel):
    id: str
    platform: Platform
    platform_video_id: str
    source_url: str
    title: str
    thumbnail_url: str | None = None
    duration: float | None = None
    uploader: str | None = None
    discovered_at: str = Field(default_factory=now_iso)
    download_state: DownloadState = DownloadState.not_queued
    error: str | None = None


class DownloadQueueItem(BaseModel):
    id: str
    source_video_id: str
    state: DownloadState = DownloadState.queued
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    error: str | None = None


class VideoAsset(BaseModel):
    id: str
    source_video_id: str
    platform: Platform
    title: str
    source_url: str
    thumbnail_url: str | None = None
    duration: float | None = None
    local_video_path: str
    created_at: str = Field(default_factory=now_iso)


class StudioState(BaseModel):
    source_videos: list[SourceVideo] = Field(default_factory=list)
    download_queue: list[DownloadQueueItem] = Field(default_factory=list)
    assets: list[VideoAsset] = Field(default_factory=list)
