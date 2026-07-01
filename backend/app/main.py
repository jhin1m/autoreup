from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.api.v1.router import api_router
from backend.app.core.config import get_settings


class SPAStaticFiles(StaticFiles):
    """Serve built assets, but fall back to index.html for unknown paths so client-side
    routes (/series/:id) survive a direct load or refresh instead of 404-ing."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AutoReup", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix=settings.api_prefix)
    app.mount("/media", StaticFiles(directory=settings.data_dir), name="media")
    app.mount("/", SPAStaticFiles(directory=settings.frontend_static_dir, html=True), name="frontend")
    return app


app = create_app()
