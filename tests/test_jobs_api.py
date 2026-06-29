import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.app.api.v1.endpoints.jobs as jobs_endpoint
from backend.app.domain.jobs import JobInput, JobManifest
from backend.app.main import create_app
from backend.app.services.job_service import JobNotFoundError


class FakeJobService:
    def __init__(self):
        self.manifest = JobManifest(schema_version="test", job_id="job123", input=JobInput(url="https://example.com/video"))

    def create(self, job_input):
        self.manifest.input = job_input
        return self.manifest

    def start_background(self, job_id):
        return None

    def get(self, job_id):
        if job_id != self.manifest.job_id:
            raise JobNotFoundError(job_id)
        return self.manifest

    def artifact_path(self, job_id, kind):
        raise JobNotFoundError(job_id)


class JobsAPITest(unittest.TestCase):
    def test_create_job_public_contract_has_no_runtime_debug_config(self):
        with patch.object(jobs_endpoint, "get_job_service", return_value=FakeJobService()):
            client = TestClient(create_app())
            created = client.post("/api/v1/jobs", json={"url": "https://example.com/video"})

        self.assertEqual(created.status_code, 202, created.text)
        job = created.json()["job"]
        self.assertEqual(job["input"], {"url": "https://example.com/video"})
        self.assertIn(job["state"], {"queued", "running", "failed"})
        self.assertEqual(
            job["config"]["stages"],
            [
                "download",
                "asr",
                "glossary",
                "translate",
                "edit",
                "qa",
                "voiceover",
                "package",
            ],
        )

    def test_unknown_artifact_returns_404(self):
        with patch.object(jobs_endpoint, "get_job_service", return_value=FakeJobService()):
            client = TestClient(create_app())
            response = client.get("/api/v1/jobs/not-found/artifacts/subtitle_vtt")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
