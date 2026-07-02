"""Contract tests for the in-process production pipeline.

No subprocess, no external services: we drive the stages with prebuilt context
artifacts and stub the LLM/render boundaries.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.core.config import Settings
from backend.app.domain.jobs import PipelineConfig, StageName
from backend.app.dubbing.models import Anchor, SpeechRow
from backend.app.pipeline.contracts import PipelineContext
from backend.app.pipeline.production import (
    ProductionPipeline,
    QAStage,
    TranslateStage,
    VoiceoverStage,
)


def _context(root: Path) -> PipelineContext:
    # Hermetic: ignore any developer .env (e.g. TTS toggles) so the test is deterministic.
    settings = Settings(root_dir=root, _env_file=None, tts_enabled=False, tts_server_url=None)
    settings.ensure_directories()
    anchors = [Anchor(0, 0.0, 2.0, "0-1", "源一"), Anchor(1, 2.0, 4.0, "2-3", "源二")]
    return PipelineContext(
        job_id="job",
        input_url="https://example.com/video",
        job_dir=settings.jobs_dir / "job",
        settings=settings,
        config=PipelineConfig(display_max_chars=settings.display_max_chars, min_duration=settings.min_duration),
        artifacts={"anchors": anchors, "glossary": [], "video": root / "v.mp4"},
    )


class ProductionPipelineTest(unittest.TestCase):
    def test_stage_order(self):
        pipeline = ProductionPipeline(Settings())
        self.assertEqual(
            [s.name for s in pipeline.stages],
            [
                StageName.download, StageName.asr, StageName.glossary, StageName.translate,
                StageName.qa, StageName.voiceover, StageName.package,
            ],
        )

    def test_translate_then_qa_produces_covered_subtitles(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            anchors = ctx.artifacts["anchors"]

            def fake_complete(system, user, config):
                return "\n".join(f"{a.id}\tLời thoại {a.id}" for a in anchors if str(a.id) in user)

            with patch("backend.app.dubbing.translate.complete", side_effect=fake_complete):
                TranslateStage().run(ctx)
            self.assertEqual({r.anchor_id for r in ctx.artifacts["speech_rows"]}, {0, 1})

            result = QAStage().run(ctx)
            self.assertEqual(result.metrics["issues"], 0)
            self.assertTrue(Path(ctx.artifacts["subtitle_srt"]).exists())

    def test_glossary_reused_from_series_file_without_llm(self):
        import json

        from backend.app.pipeline.production import GlossaryStage, _series_glossary_path

        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            ctx.series_id = "series1"
            series_path = _series_glossary_path(ctx.settings, "series1")
            series_path.write_text(json.dumps([
                {"term_zh": "罗瑞", "term_vi": "La Thụy", "kind": "name", "note": "nam"},
            ]), encoding="utf-8")

            def explode(*args, **kwargs):
                raise AssertionError("series glossary present -> the LLM must NOT be called")

            with patch("backend.app.dubbing.glossary.complete", side_effect=explode):
                result = GlossaryStage().run(ctx)
            self.assertTrue(result.metrics["reused_series_glossary"])
            self.assertEqual([g.term_vi for g in ctx.artifacts["glossary"]], ["La Thụy"])

    def test_voiceover_skipped_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))  # tts_enabled defaults to False
            ctx.artifacts["speech_rows"] = [SpeechRow(0, 0, 0.0, 2.0, "0-1", "Lời")]
            result = VoiceoverStage().run(ctx)
            self.assertEqual(result.metrics, {"enabled": False})


if __name__ == "__main__":
    unittest.main()
