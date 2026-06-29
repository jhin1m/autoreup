import tempfile
import unittest
from pathlib import Path

from backend.app.services.cookie_vault import CookieVault


class CookieFormatTest(unittest.TestCase):
    """All synthetic cookie values — no real session data."""

    def vault(self, root: str) -> CookieVault:
        return CookieVault(Path(root) / "cookies")

    def test_cookie_editor_json_object(self):
        raw = (
            '{"url":"https://www.douyin.com","cookies":['
            '{"domain":".douyin.com","name":"ttwid","value":"FAKE1","path":"/"},'
            '{"domain":".douyin.com","name":"odin_tt","value":"FAKE2","path":"/"}]}'
        )
        self.assertEqual(CookieVault._to_cookie_header(raw), "ttwid=FAKE1; odin_tt=FAKE2")

    def test_bare_json_array(self):
        raw = '[{"name":"SESSDATA","value":"abc"},{"name":"bili_jct","value":"xyz"}]'
        self.assertEqual(CookieVault._to_cookie_header(raw), "SESSDATA=abc; bili_jct=xyz")

    def test_plain_header_passthrough(self):
        self.assertEqual(CookieVault._to_cookie_header("ttwid=AAA; s_v_web_id=BBB"), "ttwid=AAA; s_v_web_id=BBB")

    def test_netscape_export(self):
        raw = ".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\tNETVAL"
        self.assertEqual(CookieVault._to_cookie_header(raw), "ttwid=NETVAL")

    def test_save_json_then_status_detects_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.vault(tmp)
            raw = '{"cookies":[{"name":"ttwid","value":"FAKE"}]}'
            status = vault.save("douyin", raw)
            self.assertTrue(status["configured"])
            self.assertIn("ttwid", status["cookie_names"])
            self.assertEqual(status["required_missing"], [])  # douyin requires ttwid

    def test_invalid_cookie_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self.vault(tmp).save("douyin", "   ")


if __name__ == "__main__":
    unittest.main()
