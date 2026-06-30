from pydantic import BaseModel, Field

from backend.app.domain.jobs import JobInput, JobManifest


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=8)
    notes: str = ""  # per-video context (cast/gender/register/terms) for the translator

    def to_job_input(self) -> JobInput:
        return JobInput(**self.model_dump())


class JobResponse(BaseModel):
    job: JobManifest
