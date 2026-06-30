"""Library REST API — series, episodes, exports. See docs/library-spec.md."""

from fastapi import APIRouter, HTTPException, Query

from backend.app.api.deps import get_library_service
from backend.app.library.models import DubState, Episode, Export, Series, SeriesPage, SeriesSource
from backend.app.library.service import LibraryError
from backend.app.schemas.library import (
    AddSourceRequest,
    CreateExportRequest,
    CreateSeriesRequest,
    TranslateRequest,
    UpdateSeriesRequest,
)

router = APIRouter()


def _series_or_404(series_id: str) -> Series:
    series = get_library_service().get_series(series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series không tồn tại")
    return series


# ----- series -------------------------------------------------------------
@router.get("/series", response_model=SeriesPage)
def list_series(q: str = "", limit: int = Query(60, le=200), offset: int = 0) -> SeriesPage:
    items, total = get_library_service().list_series(q, limit, offset)
    return SeriesPage(items=items, total=total)


@router.post("/series", response_model=Series, status_code=201)
def create_series(request: CreateSeriesRequest) -> Series:
    return get_library_service().create_series(request.title, [s.model_dump() for s in request.sources])


@router.get("/series/{series_id}", response_model=Series)
def get_series(series_id: str) -> Series:
    return _series_or_404(series_id)


@router.patch("/series/{series_id}", response_model=Series)
def update_series(series_id: str, request: UpdateSeriesRequest) -> Series:
    _series_or_404(series_id)
    return get_library_service().update_series(series_id, **request.model_dump(exclude_none=True))


@router.delete("/series/{series_id}", status_code=204)
def delete_series(series_id: str) -> None:
    if not get_library_service().delete_series(series_id):
        raise HTTPException(status_code=404, detail="Series không tồn tại")


# ----- sources ------------------------------------------------------------
@router.post("/series/{series_id}/sources", response_model=SeriesSource, status_code=201)
def add_source(series_id: str, request: AddSourceRequest) -> SeriesSource:
    _series_or_404(series_id)
    return get_library_service().add_source(series_id, request.platform, request.url, request.label)


@router.delete("/series/{series_id}/sources/{source_id}", status_code=204)
def delete_source(series_id: str, source_id: str) -> None:
    if not get_library_service().delete_source(source_id):
        raise HTTPException(status_code=404, detail="Nguồn không tồn tại")


@router.post("/series/{series_id}/scan")
def scan_series(series_id: str) -> dict:
    _series_or_404(series_id)
    try:
        return get_library_service().scan(series_id)
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ----- episodes -----------------------------------------------------------
@router.get("/series/{series_id}/episodes", response_model=list[Episode])
def list_episodes(series_id: str, dub_state: DubState | None = None) -> list[Episode]:
    _series_or_404(series_id)
    return get_library_service().list_episodes(series_id, dub_state)


@router.post("/series/{series_id}/translate")
def translate(series_id: str, request: TranslateRequest) -> dict:
    _series_or_404(series_id)
    job_ids = get_library_service().translate(series_id, request.episode_ids)
    return {"jobs": job_ids}


# ----- exports ------------------------------------------------------------
@router.get("/series/{series_id}/exports", response_model=list[Export])
def list_exports(series_id: str) -> list[Export]:
    _series_or_404(series_id)
    return get_library_service().list_exports(series_id)


@router.post("/series/{series_id}/exports", response_model=Export, status_code=201)
def create_export(series_id: str, request: CreateExportRequest) -> Export:
    _series_or_404(series_id)
    try:
        return get_library_service().create_export(
            series_id, request.title, request.description, request.hashtags, request.episode_job_ids
        )
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/exports/{export_id}/render", response_model=Export)
def render_export(export_id: str) -> Export:
    export = get_library_service().start_render(export_id)
    if not export:
        raise HTTPException(status_code=404, detail="Bản ghép không tồn tại")
    return export


@router.get("/exports/{export_id}", response_model=Export)
def get_export(export_id: str) -> Export:
    export = get_library_service().get_export(export_id)
    if not export:
        raise HTTPException(status_code=404, detail="Bản ghép không tồn tại")
    return export


@router.delete("/exports/{export_id}", status_code=204)
def delete_export(export_id: str) -> None:
    if not get_library_service().delete_export(export_id):
        raise HTTPException(status_code=404, detail="Bản ghép không tồn tại")
