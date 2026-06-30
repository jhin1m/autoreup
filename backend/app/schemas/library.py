"""Request bodies for the library API. Responses reuse the models in library.models."""

from pydantic import BaseModel, Field


class SourceInput(BaseModel):
    platform: str
    url: str
    label: str | None = None


class CreateSeriesRequest(BaseModel):
    title: str
    sources: list[SourceInput] = Field(default_factory=list)


class UpdateSeriesRequest(BaseModel):
    title: str | None = None
    tags: list[str] | None = None
    cover_url: str | None = None


class AddSourceRequest(BaseModel):
    platform: str
    url: str
    label: str | None = None


class TranslateRequest(BaseModel):
    episode_ids: list[str]


class CreateExportRequest(BaseModel):
    title: str
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    episode_job_ids: list[str]


class PreviewChannelRequest(BaseModel):
    url: str


class ImportGroup(BaseModel):
    title: str
    platform: str | None = None
    series_id: str | None = None   # set → sync into existing series; null → create new
    episodes: list[dict] = Field(default_factory=list)


class ImportChannelRequest(BaseModel):
    url: str
    groups: list[ImportGroup]
