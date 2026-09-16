import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import asr_worker_client


class ASRWorkerClientContractTest(unittest.TestCase):
    def test_call_worker_sends_selected_model(self):
        payloads = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'{"segments": []}'

        def fake_urlopen(request, timeout):
            payloads.append(json.loads(request.data.decode()))
            return FakeResponse()

        with tempfile.NamedTemporaryFile(suffix=".m4a") as audio:
            audio.write(b"fake-audio-bytes")
            audio.flush()
            with patch.object(asr_worker_client, "urlopen", fake_urlopen):
                asr_worker_client.call_worker(
                    "http://worker",
                    Path(audio.name),
                    offset=12.0,
                    length=30.0,
                    language="zh",
                    model="@cf/openai/whisper",
                )

        self.assertEqual(payloads[0]["model"], "@cf/openai/whisper")


if __name__ == "__main__":
    unittest.main()


def test_local_backend_model_mapping_and_auto_selection():
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts import asr_worker_client as client

    # Cloudflare slugs map onto the MLX repos (same weights, local runtime)
    assert client.local_repo("@cf/openai/whisper-large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"
    assert client.local_repo("@cf/openai/whisper") == "mlx-community/whisper-large-v3-mlx"
    assert client.local_repo("mlx-community/whisper-small-mlx") == "mlx-community/whisper-small-mlx"
    # auto picks local exactly when mlx-whisper is importable
    expected = "local" if importlib.util.find_spec("mlx_whisper") else "worker"
    assert client.pick_backend("auto") == expected
    assert client.pick_backend("worker") == "worker"
    assert client.pick_backend("local") == "local"
