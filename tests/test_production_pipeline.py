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
    EditStage,
    ProductionPipeline,
    QAStage,
    TranslateStage,
    VoiceoverStage,
)


def _context(root: Path) -> PipelineContext:
    settings = Settings(root_dir=root)
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
                StageName.edit, StageName.qa, StageName.voiceover, StageName.package,
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

    def test_edit_stage_is_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            ctx.artifacts["speech_rows"] = [SpeechRow(0, 0, 0.0, 2.0, "0-1", "Lời gốc")]
            with patch("backend.app.dubbing.editor.complete", return_value="PATCH\t0\tLời mới"):
                result = EditStage().run(ctx)
            # patch without a finding is ignored
            self.assertEqual(result.metrics["patched_anchors"], 0)

    def test_voiceover_skipped_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))  # tts_enabled defaults to False
            ctx.artifacts["speech_rows"] = [SpeechRow(0, 0, 0.0, 2.0, "0-1", "Lời")]
            result = VoiceoverStage().run(ctx)
            self.assertEqual(result.metrics, {"enabled": False})


if __name__ == "__main__":
    unittest.main()
