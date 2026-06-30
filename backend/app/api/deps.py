from functools import lru_cache

from backend.app.core.config import get_settings
from backend.app.services.job_service import JobService


@lru_cache
def get_job_service() -> JobService:
    return JobService(get_settings())


@lru_cache
def get_library_service():
    from backend.app.library.service import LibraryService

    return LibraryService(get_settings(), get_job_service())
