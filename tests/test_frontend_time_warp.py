import subprocess
import unittest

from tests.test_settings_ui_model import NODE, ROOT, _node_major


@unittest.skipUnless(NODE and _node_major() >= 23, "cần Node >= 23 (tự strip type TypeScript)")
class FrontendTimeWarpTest(unittest.TestCase):
    """The preview/editor clock map for a stretched render (frontend/src/util.ts)."""

    def test_node_suite_passes(self):
        result = subprocess.run(
            [NODE, "--test", "--no-warnings", str(ROOT / "frontend/tests/time-warp.test.mjs")],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
