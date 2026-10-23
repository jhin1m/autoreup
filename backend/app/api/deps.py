import threading
from functools import lru_cache

from backend.app.core.config import get_settings
from backend.app.services.job_service import JobService

# lru_cache alone is not a singleton: threads that miss the cache together each run the
# builder. The page fires several requests at once after a server restart, so the server
# ran two JobServices — the resumed job lived in one and Stop looked it up in the other,
# never reaching the render. RLock: building the library service builds the job service.
_build_lock = threading.RLock()


@lru_cache
def _job_service() -> JobService:
    return JobService(get_settings())


@lru_cache
def _library_service():
    from backend.app.library.service import LibraryService

    return LibraryService(get_settings(), get_job_service())


def get_job_service() -> JobService:
    with _build_lock:
        return _job_service()


def get_library_service():
    with _build_lock:
        return _library_service()
