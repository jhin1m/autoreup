import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.services.cookie_vault import CookieVault


class StudioAPITest(unittest.TestCase):
    def test_provider_sessions_endpoint_hides_cookie_values(self):
        client = TestClient(create_app())
        response = client.get("/api/v1/providers/sessions")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("sessions", payload)
        self.assertNotIn("Cookie", response.text)
        self.assertNotIn("ttwid=", response.text)

    def test_cookie_vault_stores_private_cookie_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = CookieVault(Path(tmp))
            status = vault.save("douyin", "ttwid=abc; msToken=def; s_v_web_id=ghi")

            self.assertTrue(status["configured"])
            self.assertEqual(status["required_missing"], [])
            self.assertEqual(vault.read_header("douyin"), "ttwid=abc; msToken=def; s_v_web_id=ghi")
            self.assertEqual(oct((Path(tmp) / "douyin.cookie").stat().st_mode & 0o777), "0o600")

    def test_studio_state_endpoint_has_personal_workflow_sections(self):
        client = TestClient(create_app())
        response = client.get("/api/v1/studio/state")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("source_videos", payload)
        self.assertIn("download_queue", payload)
        self.assertIn("assets", payload)


if __name__ == "__main__":
    unittest.main()
