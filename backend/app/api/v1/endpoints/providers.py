from fastapi import APIRouter, HTTPException

from backend.app.core.config import get_settings
from backend.app.schemas.providers import CookieImportRequest, ProviderSessionStatus, ProviderSessionsResponse
from backend.app.services.cookie_vault import CookieVault


router = APIRouter()


def get_cookie_vault() -> CookieVault:
    return CookieVault(get_settings().provider_cookies_dir)


@router.get("/sessions", response_model=ProviderSessionsResponse)
def provider_sessions() -> ProviderSessionsResponse:
    return ProviderSessionsResponse(sessions=[ProviderSessionStatus(**item) for item in get_cookie_vault().status_all()])


@router.put("/{platform}/cookies", response_model=ProviderSessionStatus)
def save_provider_cookies(platform: str, request: CookieImportRequest) -> ProviderSessionStatus:
    try:
        return ProviderSessionStatus(**get_cookie_vault().save(platform, request.cookies))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{platform}/cookies", response_model=ProviderSessionStatus)
def clear_provider_cookies(platform: str) -> ProviderSessionStatus:
    try:
        return ProviderSessionStatus(**get_cookie_vault().clear(platform))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
