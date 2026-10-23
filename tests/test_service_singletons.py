import threading
import time

import backend.app.api.deps as deps


def test_requests_racing_at_startup_share_one_job_service(monkeypatch):
    # two JobServices meant the job ran in one while Stop looked in the other
    built = []

    class SlowJobService:
        def __init__(self, settings):
            time.sleep(0.05)  # reconciling orphaned jobs takes a moment
            built.append(self)

    monkeypatch.setattr(deps, "JobService", SlowJobService)
    deps._job_service.cache_clear()
    try:
        seen = []
        threads = [threading.Thread(target=lambda: seen.append(deps.get_job_service())) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(built) == 1
        assert all(service is built[0] for service in seen)
    finally:
        deps._job_service.cache_clear()
