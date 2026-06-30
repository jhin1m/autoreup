"""Library domain models — Series → Episode → Export.

These are the API/DTO shapes (pydantic) the store reads/writes. Persistence lives in
SQLite (see ``db.py``/``store.py``); these models are the boundary the service and the
HTTP layer speak. See docs/library-spec.md for the locked data model.
"""

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field

from backend.app.studio.models import Platform


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DubState(StrEnum):
    """How far an episode has progressed through dubbing (cached from its job)."""

    none = "none"            # not translated yet
    translating = "translating"
    dubbed = "dubbed"
    failed = "failed"


class ExportState(StrEnum):
    draft = "draft"          # episodes picked, not rendered
    rendering = "rendering"
    done = "done"
    failed = "failed"


class SeriesSource(BaseModel):
    """A channel/playlist link feeding a series (one per platform account)."""

    id: str
    series_id: str
    platform: Platform
    url: str
    label: str | None = None
    last_scanned_at: str | None = None


class Episode(BaseModel):
    """A source video belonging to exactly one series (locked 1→1)."""

    id: str
    series_id: str
    platform: Platform
    platform_video_id: str
    source_url: str
    title: str
    thumbnail_url: str | None = None
    duration: float | None = None
    uploader: str | None = None
    discovered_at: str = Field(default_factory=now_iso)
    dub_job_id: str | None = None
    dub_state: DubState = DubState.none


class Series(BaseModel):
    id: str
    title: str
    cover_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    # Denormalised counts, filled on read (not stored).
    episode_count: int = 0
    dubbed_count: int = 0
    export_count: int = 0
    sources: list[SeriesSource] = Field(default_factory=list)


class Export(BaseModel):
    """A compilation: an ordered set of dubbed episodes concatenated into one video."""

    id: str
    series_id: str
    title: str
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    episode_job_ids: list[str] = Field(default_factory=list)  # ordered
    output_path: str | None = None   # relative to data_dir, served via /media
    duration: float | None = None
    state: ExportState = ExportState.draft
    error: str | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class SeriesPage(BaseModel):
    """A page of series for the library grid."""

    items: list[Series]
    total: int
