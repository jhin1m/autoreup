from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class ArtifactKind(StrEnum):
    video = "video"
    source_json = "source_json"
    subtitle_json = "subtitle_json"
    subtitle_srt = "subtitle_srt"
    subtitle_vtt = "subtitle_vtt"
    qa_report = "qa_report"
    glossary = "glossary"
    pipeline_audit = "pipeline_audit"
    tts_manifest = "tts_manifest"
    voice_track = "voice_track"
    soft_sub_mp4 = "soft_sub_mp4"
    dubbed_mp4 = "dubbed_mp4"
    zip_package = "zip_package"


class Artifact(BaseModel):
    kind: ArtifactKind
    path: str
    mime_type: str
    size_bytes: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    meta: dict = Field(default_factory=dict)
