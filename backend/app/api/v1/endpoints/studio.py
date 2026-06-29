from fastapi import APIRouter, HTTPException

from backend.app.core.config import get_settings
from backend.app.schemas.studio import DiscoverRequest, DiscoverResponse, QueueDownloadsRequest, StudioStateResponse
from backend.app.studio.service import StudioService


router = APIRouter()


def get_studio_service() -> StudioService:
    return StudioService(get_settings())


@router.get("/state", response_model=StudioStateResponse)
def studio_state() -> StudioStateResponse:
    return StudioStateResponse(**get_studio_service().state().model_dump())


@router.post("/discover", response_model=DiscoverResponse)
def discover_source(request: DiscoverRequest) -> DiscoverResponse:
    try:
        return DiscoverResponse(videos=get_studio_service().discover(request.url, request.limit))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không thể quét nguồn này: {exc}") from exc


@router.post("/download-queue", response_model=StudioStateResponse)
def queue_downloads(request: QueueDownloadsRequest) -> StudioStateResponse:
    state = get_studio_service().enqueue_downloads(request.video_ids)
    return StudioStateResponse(**state.model_dump())
