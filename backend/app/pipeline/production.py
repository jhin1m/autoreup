"""Production dubbing pipeline — in-process, no subprocess to prototype scripts.

Stage order::

    download -> asr -> glossary -> translate -> edit -> qa -> voiceover -> package

Each stage calls :mod:`backend.app.dubbing` functions and passes typed objects
through ``context.artifacts``. TSV/JSON files are written only as inspectable
artifacts. The single external adapter still shelled out to is the ASR worker
client (an external-service adapter, like yt-dlp/ffmpeg).
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from backend.app.core.config import Settings
from backend.app.domain.artifacts import Artifact, ArtifactKind
from backend.app.domain.jobs import JobInput, PipelineConfig, StageName
from backend.app.dubbing.chunking import ChunkConfig, build_chunks, flat_scene_map
from backend.app.dubbing.display import build_display_cues, write_subtitles
from backend.app.dubbing.editor import EditorConfig, edit_speech
from backend.app.dubbing.glossary import extract_glossary, format_glossary
from backend.app.dubbing.llm import LLMConfig
from backend.app.dubbing.models import (
    ANCHOR_FIELDS,
    SEGMENT_FIELDS,
    SPEECH_FIELDS,
    Anchor,
    GlossaryEntry,
    SpeechRow,
)
from backend.app.dubbing.qa import qa_report
from backend.app.dubbing.rail import AnchorConfig, build_anchors, segments_from_srt
from backend.app.dubbing.render import RenderConfig, assemble_voice, render
from backend.app.dubbing.repair import RepairConfig, expand, repair
from backend.app.dubbing.translate import TranslateConfig, translate
from backend.app.dubbing.tsv_io import write_json, write_tsv
from backend.app.infrastructure.downloader import YtDlpDownloader
from backend.app.infrastructure.packaging import package_outputs
from backend.app.infrastructure.process import run_command
from backend.app.pipeline.contracts import PipelineContext, PipelineStage, StageResult

_VOICEOVER_REPAIR_ROUNDS = 3


class ProductionPipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stages: list[PipelineStage] = [
            DownloadStage(),
            ASRStage(),
            GlossaryStage(),
            TranslateStage(),
            EditStage(),
            QAStage(),
            VoiceoverStage(),
            PackageStage(),
        ]

    def create_context(self, job_id: str, job_input: JobInput, config: PipelineConfig) -> PipelineContext:
        job_dir = self.settings.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return PipelineContext(
            job_id=job_id,
            input_url=job_input.url,
            job_dir=job_dir,
            settings=self.settings,
            config=config,
            artifacts={},
        )


def _translate_llm(settings: Settings) -> LLMConfig:
    return LLMConfig(provider=settings.llm_provider, base_url=settings.llm_base_url, model=settings.translate_model, stream=False)


def _polish_llm(settings: Settings) -> LLMConfig:
    return LLMConfig(provider=settings.llm_provider, base_url=settings.llm_base_url, model=settings.polish_model, stream=False)


class DownloadStage(PipelineStage):
    name = StageName.download

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        result = _download_source(settings, context.input_url, context.job_id)
        video = Path(result["path"])
        context.artifacts["video"] = video
        return StageResult(
            name=self.name,
            artifacts=[_artifact(ArtifactKind.video, video, settings.data_dir)],
            metrics={
                "title": result.get("title"),
                "duration": result.get("duration"),
                "thumbnail_url": result.get("thumbnail_url"),
                "uploader": result.get("uploader"),
                "webpage_url": result.get("webpage_url"),
            },
        )


class ASRStage(PipelineStage):
    name = StageName.asr

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        if not settings.asr_worker_url:
            raise RuntimeError("Missing ASR worker URL. Set AUTOREUP_ASR_WORKER_URL.")
        output_srt = settings.subtitles_dir / f"{context.job_id}.asr.zh.srt"
        logs = _run_asr(context, output_srt)

        segments = segments_from_srt(output_srt)
        anchors = build_anchors(segments, AnchorConfig())
        context.artifacts["segments"] = segments
        context.artifacts["anchors"] = anchors

        segments_tsv = settings.subtitles_dir / f"{context.job_id}.segments.tsv"
        anchors_tsv = settings.subtitles_dir / f"{context.job_id}.anchors.tsv"
        write_tsv(segments_tsv, [s.to_row() for s in segments], SEGMENT_FIELDS)
        write_tsv(anchors_tsv, [a.to_row() for a in anchors], ANCHOR_FIELDS)
        return StageResult(
            name=self.name,
            artifacts=[_artifact(ArtifactKind.source_json, anchors_tsv, settings.data_dir)],
            metrics={"segments": len(segments), "anchors": len(anchors)},
            logs=logs,
        )


class GlossaryStage(PipelineStage):
    name = StageName.glossary

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        anchors: list[Anchor] = context.artifacts["anchors"]
        glossary = extract_glossary(anchors, _translate_llm(settings), genre=context.config.content_genre)
        context.artifacts["glossary"] = glossary
        rows = [{"term_zh": g.term_zh, "term_vi": g.term_vi, "kind": g.kind, "note": g.note} for g in glossary]
        tsv_path = settings.subtitles_dir / f"{context.job_id}.glossary.tsv"
        json_path = settings.subtitles_dir / f"{context.job_id}.glossary.json"
        write_tsv(tsv_path, [{k.upper(): v for k, v in row.items()} for row in rows], ["TERM_ZH", "TERM_VI", "KIND", "NOTE"])
        write_json(json_path, rows)
        return StageResult(
            name=self.name,
            artifacts=[_artifact(ArtifactKind.glossary, json_path, settings.data_dir)],
            metrics={"terms": len(glossary), "by_kind": _count_kinds(glossary)},
            logs=[format_glossary(glossary)[:2000]],
        )


class TranslateStage(PipelineStage):
    name = StageName.translate

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        anchors: list[Anchor] = context.artifacts["anchors"]
        glossary: list[GlossaryEntry] = context.artifacts.get("glossary", [])
        chunks = build_chunks(anchors, flat_scene_map(anchors), ChunkConfig())
        speech_rows = translate(
            anchors, chunks, glossary, _translate_llm(settings),
            TranslateConfig(max_caption_chars=settings.display_max_chars),
        )
        context.artifacts["chunks"] = chunks
        context.artifacts["speech_rows"] = speech_rows
        path = settings.subtitles_dir / f"{context.job_id}.speech.tsv"
        write_tsv(path, [r.to_row() for r in speech_rows], SPEECH_FIELDS)
        return StageResult(
            name=self.name,
            artifacts=[_artifact(ArtifactKind.subtitle_json, path, settings.data_dir)],
            metrics={"chunks": len(chunks), "speech_rows": len(speech_rows)},
        )


class EditStage(PipelineStage):
    name = StageName.edit

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        anchors: list[Anchor] = context.artifacts["anchors"]
        glossary: list[GlossaryEntry] = context.artifacts.get("glossary", [])
        speech_rows: list[SpeechRow] = context.artifacts["speech_rows"]
        edited, patched = edit_speech(
            speech_rows, anchors, glossary, _polish_llm(settings),
            EditorConfig(max_caption_chars=settings.display_max_chars),
        )
        context.artifacts["speech_rows"] = edited
        path = settings.subtitles_dir / f"{context.job_id}.speech.edited.tsv"
        write_tsv(path, [r.to_row() for r in edited], SPEECH_FIELDS)
        return StageResult(
            name=self.name,
            artifacts=[_artifact(ArtifactKind.subtitle_json, path, settings.data_dir)],
            metrics={"patched_anchors": patched, "speech_rows": len(edited)},
        )


class QAStage(PipelineStage):
    name = StageName.qa

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        anchors: list[Anchor] = context.artifacts["anchors"]
        speech_rows: list[SpeechRow] = context.artifacts["speech_rows"]
        cues = build_display_cues(speech_rows, settings.min_duration, anchors, context.artifacts.get("segments"))
        report = qa_report(speech_rows, anchors, cues, max_display_chars=settings.display_max_chars)

        subtitle_json = settings.subtitles_dir / f"{context.job_id}.vi.display.json"
        subtitle_srt = settings.subtitles_dir / f"{context.job_id}.vi.display.srt"
        write_subtitles(cues, subtitle_json, subtitle_srt)
        qa_json = settings.subtitles_dir / f"{context.job_id}.qa.json"
        write_json(qa_json, report)

        context.artifacts["display_cues"] = cues
        context.artifacts["subtitle_json"] = subtitle_json
        context.artifacts["subtitle_srt"] = subtitle_srt
        if report["summary"]["issues"]:
            raise RuntimeError(f"QA failed: {report['summary']['issue_detail']}")
        return StageResult(
            name=self.name,
            artifacts=[
                _artifact(ArtifactKind.subtitle_json, subtitle_json, settings.data_dir),
                _artifact(ArtifactKind.subtitle_srt, subtitle_srt, settings.data_dir),
                _artifact(ArtifactKind.qa_report, qa_json, settings.data_dir),
            ],
            metrics=report["summary"] | {"coverage": report["coverage"]},
        )


class VoiceoverStage(PipelineStage):
    name = StageName.voiceover

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        if not settings.tts_enabled:
            return StageResult(name=self.name, metrics={"enabled": False}, logs=["TTS disabled; set AUTOREUP_TTS_ENABLED=true"])
        if not settings.tts_server_url:
            raise RuntimeError("Missing TTS server URL. Set AUTOREUP_TTS_SERVER_URL.")

        anchors: list[Anchor] = context.artifacts["anchors"]
        glossary: list[GlossaryEntry] = context.artifacts.get("glossary", [])
        speech_rows: list[SpeechRow] = context.artifacts["speech_rows"]
        render_config = RenderConfig(
            provider_url=settings.tts_server_url,
            speaker=settings.tts_voice,
            text_mode=settings.tts_text_mode,
            min_tempo=settings.tts_min_tempo,
            max_tempo=settings.tts_max_tempo,
            fixed_tempo=settings.tts_fixed_tempo,
        )
        work_dir = context.job_dir / "tts"
        cache_dir = settings.tts_cache_dir
        output_audio = settings.tts_dir / f"{context.job_id}.voice.m4a"

        result = render(speech_rows, anchors, render_config, work_dir, cache_dir)
        logs = [f"render round 0: {result.summary}"]
        repair_cfg = RepairConfig(max_caption_chars=settings.display_max_chars)
        rounds = 0
        while rounds < _VOICEOVER_REPAIR_ROUNDS:
            # Overflow: voice overruns the window -> shorten. Underfull: voice far
            # shorter than the source (often dropped content) -> complete it.
            overflow = {t.anchor_id: (t.target_duration, t.tts_duration) for t in result.timings if t.needs_repair}
            underfull = {
                t.anchor_id: (t.target_duration, t.tts_duration)
                for t in result.timings
                if t.target_duration >= 2.0 and t.tts_duration < t.target_duration * 0.6
            }
            if not overflow and not underfull:
                break
            changed = 0
            if overflow:
                speech_rows, n = repair(speech_rows, anchors, glossary, overflow, _polish_llm(settings), repair_cfg)
                changed += n
                logs.append(f"round {rounds + 1}: shortened {n} overflow anchors")
            if underfull:
                speech_rows, n = expand(speech_rows, anchors, glossary, underfull, _polish_llm(settings), repair_cfg)
                changed += n
                logs.append(f"round {rounds + 1}: completed {n} underfull anchors")
            rounds += 1
            if not changed:
                break
            result = render(speech_rows, anchors, render_config, work_dir, cache_dir)
            logs.append(f"render round {rounds}: {result.summary}")

        # Assemble the final track once, after timings have converged.
        assemble_voice(result, output_audio, work_dir)
        context.artifacts["speech_rows"] = speech_rows
        cues = build_display_cues(speech_rows, settings.min_duration, anchors, context.artifacts.get("segments"))
        write_subtitles(cues, context.artifacts["subtitle_json"], context.artifacts["subtitle_srt"])
        context.artifacts["display_cues"] = cues

        manifest = settings.tts_dir / f"{context.job_id}.tts.json"
        write_json(manifest, {"summary": result.summary, "timings": [asdict(t) for t in result.timings]})
        artifacts = []
        if result.voice_track:
            context.artifacts["voice_track"] = result.voice_track
            artifacts.append(_artifact(ArtifactKind.voice_track, result.voice_track, settings.data_dir))
        artifacts.append(_artifact(ArtifactKind.tts_manifest, manifest, settings.data_dir))
        return StageResult(name=self.name, artifacts=artifacts, metrics={"enabled": True, "repair_rounds": rounds, **result.summary}, logs=logs)


class PackageStage(PipelineStage):
    name = StageName.package

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        voice_track = context.artifacts.get("voice_track")
        artifacts, logs = package_outputs(
            video=Path(context.artifacts["video"]),
            subtitle_json=Path(context.artifacts["subtitle_json"]),
            subtitle_srt=Path(context.artifacts["subtitle_srt"]),
            packages_dir=settings.packages_dir,
            root_data_dir=settings.data_dir,
            voice_track=Path(voice_track) if voice_track else None,
        )
        return StageResult(name=self.name, artifacts=artifacts, logs=logs)


def _artifact(kind: ArtifactKind, path: Path, data_dir: Path) -> Artifact:
    mime = {
        ArtifactKind.video: "video/mp4",
        ArtifactKind.source_json: "text/tab-separated-values",
        ArtifactKind.subtitle_json: "application/json",
        ArtifactKind.subtitle_srt: "application/x-subrip",
        ArtifactKind.qa_report: "application/json",
        ArtifactKind.glossary: "application/json",
        ArtifactKind.pipeline_audit: "text/tab-separated-values",
        ArtifactKind.tts_manifest: "application/json",
        ArtifactKind.voice_track: "audio/mp4",
    }.get(kind, "application/octet-stream")
    return Artifact(
        kind=kind,
        path=str(path.relative_to(data_dir)),
        mime_type=mime,
        size_bytes=path.stat().st_size if path.exists() else None,
    )


def _download_source(settings: Settings, url: str, job_id: str) -> dict:
    """Douyin goes through the headless-browser extractor; everything else via yt-dlp."""
    from backend.app.services.cookie_vault import CookieVault
    from backend.app.studio.providers import detect_platform

    vault = CookieVault(settings.provider_cookies_dir)
    platform = detect_platform(url)
    if platform.value == "douyin":
        from backend.app.infrastructure.browser_extractor import DouyinBrowserExtractor

        extractor = DouyinBrowserExtractor(settings.videos_dir, vault, settings.root_dir, settings.browser_path, settings.node_bin)
        if extractor.available():
            return extractor.download(url, job_id)

    cookies_file = (vault.netscape_file(platform.value) if platform.value in {"douyin", "bilibili"} else None)
    return YtDlpDownloader(settings.videos_dir).download(url, str(cookies_file) if cookies_file else settings.cookies_file, job_id)


def _count_kinds(glossary: list[GlossaryEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in glossary:
        key = entry.kind or "term"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _run_asr(context: PipelineContext, output_srt: Path) -> list[str]:
    settings: Settings = context.settings
    script = settings.scripts_dir / "asr_worker_client.py"
    command = [
        sys.executable, str(script), str(context.artifacts["video"]),
        "--worker-url", str(settings.asr_worker_url),
        "--output", str(output_srt),
        "--language", "zh",
        "--model", str(settings.asr_model),
        "--workers", "4",
        "--max-chars", "18",
    ]
    return run_command(command, cwd=settings.root_dir)
