import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from backend.app.api.deps import get_job_service
from backend.app.domain.artifacts import ArtifactKind
from backend.app.domain.jobs import JobState
from backend.app.schemas.jobs import CreateJobRequest, JobResponse
from backend.app.services.job_service import JobBusyError, JobNotFoundError


router = APIRouter()


@router.post("", response_model=JobResponse, status_code=202)
def create_job(request: CreateJobRequest) -> JobResponse:
    service = get_job_service()
    manifest = service.create(request.to_job_input())
    service.start_background(manifest.job_id)
    return JobResponse(job=manifest)


@router.get("", response_model=list[JobResponse])
def list_jobs() -> list[JobResponse]:
    return [JobResponse(job=job) for job in get_job_service().list_recent()]


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    try:
        return JobResponse(job=get_job_service().get(job_id))
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(job_id: str) -> JobResponse:
    try:
        return JobResponse(job=get_job_service().request_cancel(job_id))
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    try:
        get_job_service().delete(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="Job đang chạy — hãy Dừng trước khi xóa.") from exc


@router.get("/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    async def stream():
        last_payload = ""
        while True:
            try:
                manifest = get_job_service().get(job_id)
            except JobNotFoundError:
                yield "event: error\ndata: {\"error\": \"Job not found\"}\n\n"
                return
            payload = manifest.model_dump_json()
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            if manifest.state in {JobState.succeeded, JobState.failed, JobState.canceled}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/{job_id}/artifacts/{kind}")
def download_artifact(job_id: str, kind: ArtifactKind) -> FileResponse:
    try:
        path, mime_type = get_job_service().artifact_path(job_id, kind)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    return FileResponse(path, media_type=mime_type, filename=path.name)


@router.get("/{job_id}/artifacts")
def list_artifacts(job_id: str) -> dict:
    try:
        manifest = get_job_service().get(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return {"artifacts": [json.loads(artifact.model_dump_json()) for artifact in manifest.artifacts]}
