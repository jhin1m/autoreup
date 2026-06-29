import json
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

        with patch.object(asr_worker_client, "urlopen", fake_urlopen):
            asr_worker_client.call_worker(
                "http://worker",
                Path("data/subtitles/e07f29ec62bc.asr.zh.srt"),
                offset=12.0,
                length=30.0,
                language="zh",
                model="@cf/openai/whisper",
            )

        self.assertEqual(payloads[0]["model"], "@cf/openai/whisper")


if __name__ == "__main__":
    unittest.main()
