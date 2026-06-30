from fastapi import APIRouter

from backend.app.api.v1.endpoints import jobs, library, providers, studio


api_router = APIRouter()
api_router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
api_router.include_router(providers.router, prefix="/providers", tags=["providers"])
api_router.include_router(studio.router, prefix="/studio", tags=["studio"])
api_router.include_router(library.router, prefix="/library", tags=["library"])
