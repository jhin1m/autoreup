from functools import lru_cache

from backend.app.core.config import get_settings
from backend.app.services.job_service import JobService


@lru_cache
def get_job_service() -> JobService:
    return JobService(get_settings())
