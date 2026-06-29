from pydantic import BaseModel, Field

from backend.app.studio.models import DownloadQueueItem, SourceVideo, VideoAsset


class DiscoverRequest(BaseModel):
    url: str = Field(min_length=8)
    limit: int = Field(default=12, ge=1, le=50)


class DiscoverResponse(BaseModel):
    videos: list[SourceVideo]


class QueueDownloadsRequest(BaseModel):
    video_ids: list[str] = Field(min_length=1)


class StudioStateResponse(BaseModel):
    source_videos: list[SourceVideo]
    download_queue: list[DownloadQueueItem]
    assets: list[VideoAsset]
