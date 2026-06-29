import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTest(unittest.TestCase):
    def test_frontend_stack_is_react_spa(self):
        package = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
        dependencies = package["dependencies"]
        self.assertIn("react", dependencies)
        self.assertIn("react-dom", dependencies)
        self.assertIn("vite", dependencies)
        # The pipeline view is a bespoke transport rail, not a generic node-graph lib.
        self.assertNotIn("@xyflow/react", dependencies)

    def test_user_ui_does_not_expose_runtime_debug_config(self):
        forbidden = ["sample://golden", "ASR worker", "LLM provider", "gpt-5", "debug", "Golden demo"]
        source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "frontend/src").rglob("*.*") if path.suffix in {".tsx", ".ts", ".css"})
        for text in forbidden:
            self.assertNotIn(text, source)


if __name__ == "__main__":
    unittest.main()
