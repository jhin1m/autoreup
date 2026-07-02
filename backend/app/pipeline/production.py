"""Production dubbing pipeline — in-process, no subprocess to prototype scripts.

Stage order::

    download -> asr -> glossary -> translate -> qa -> voiceover -> package

Each stage calls :mod:`backend.app.dubbing` functions and passes typed objects
through ``context.artifacts``. TSV/JSON files are written only as inspectable
artifacts. The single external adapter still shelled out to is the ASR worker
client (an external-service adapter, like yt-dlp/ffmpeg).
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from backend.app.core.config import Settings
from backend.app.domain.artifacts import Artifact, ArtifactKind
from backend.app.domain.jobs import JobInput, PipelineConfig, StageName
from backend.app.dubbing.chunking import ChunkConfig, build_chunks, flat_scene_map
from backend.app.dubbing.display import build_display_cues, write_subtitles
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
from backend.app.services.audio import plan_placements
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
            series_id=job_input.series_id,
        )


def _translate_llm(settings: Settings) -> LLMConfig:
    return LLMConfig(provider=settings.llm_provider, base_url=settings.llm_base_url, model=settings.translate_model, stream=False)


def _polish_llm(settings: Settings) -> LLMConfig:
    return LLMConfig(provider=settings.llm_provider, base_url=settings.llm_base_url, model=settings.polish_model, stream=False)


def _hardsub_config(settings: Settings):
    from backend.app.services.hardsub import HardsubConfig
    from backend.app.infrastructure.ffmpeg_encode import video_encode_args, canonical_scale_filter

    font = settings.hardsub_font or str(settings.root_dir / "backend" / "app" / "assets" / "fonts" / "Bangers-Regular.ttf")
    return HardsubConfig(
        font_path=font,
        font_size=settings.hardsub_font_size,
        blur=settings.hardsub_blur,
        band_pad=settings.hardsub_band_pad,
        darken=settings.hardsub_darken,
        bed_volume=settings.dub_bed_volume,
        video_codec_args=tuple(video_encode_args(settings)),
        scale_filter=canonical_scale_filter(settings),
    )


def _build_capcut_client(settings: Settings):
    """Native in-process CapCut TTS client, or None to fall back to the HTTP server.

    Self-contained: mints/refreshes its own CapCut session via email/password login
    (pure Python) when the session file is missing or stale, then signs + batches
    synthesis itself. Returns None only if native mode is off or no session can be
    obtained (then the pipeline uses ``tts_server_url`` as before)."""
    if not settings.tts_native:
        return None
    session_path = Path(settings.capcut_session_file) if settings.capcut_session_file else settings.private_dir / "capcut" / "capcut-session.json"

    stale = (not session_path.exists()) or (
        time.time() - session_path.stat().st_mtime > settings.capcut_session_max_age_days * 86400
    )
    if stale and settings.capcut_email and settings.capcut_password:
        try:
            from backend.app.services.capcut_login import LoginConfig, login

            login(LoginConfig(
                email=settings.capcut_email,
                password=settings.capcut_password,
                session_file=session_path,
                app_id=settings.capcut_app_id,
                web_url=settings.capcut_web_url,
                login_host=settings.capcut_login_host,
                fallback_login_host=settings.capcut_fallback_login_host,
                page_locale=settings.capcut_page_locale,
                locale=settings.capcut_locale,
                store_country=settings.capcut_store_country,
                user_agent=settings.capcut_user_agent,
            ))
        except Exception:
            pass  # fall back to an existing (possibly stale) session if present

    if not session_path.exists():
        return None
    from backend.app.services.capcut_tts import CapCutConfig, CapCutTtsClient

    try:
        return CapCutTtsClient(CapCutConfig(
            session_file=session_path,
            edit_api=settings.capcut_edit_api,
            web_url=settings.capcut_web_url,
            app_id=settings.capcut_app_id,
            app_version=settings.capcut_app_version,
            platform_id=settings.capcut_platform_id,
            sign_version=settings.capcut_sign_version,
            sign_prefix=settings.capcut_sign_prefix,
            sign_suffix=settings.capcut_sign_suffix,
            sign_path_tail=settings.capcut_sign_path_tail,
            store_country=settings.capcut_store_country,
            user_agent=settings.capcut_user_agent,
            speaker=settings.tts_voice,
            effect_id=settings.capcut_effect_id,
            resource_id=settings.capcut_resource_id,
            voice_name=settings.capcut_voice_name,
        ))
    except Exception:
        return None


class DownloadStage(PipelineStage):
    name = StageName.download

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        result = _download_source(settings, context.input_url, context.job_id)
        video = Path(result["path"])
        context.artifacts["video"] = video
        artifacts = [_artifact(ArtifactKind.video, video, settings.data_dir)]
        thumb = _make_thumbnail(video, settings.videos_dir / f"{context.job_id}.thumb.jpg")
        if thumb is not None:
            artifacts.append(_artifact(ArtifactKind.thumbnail, thumb, settings.data_dir))
        return StageResult(
            name=self.name,
            artifacts=artifacts,
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

        # Episodes of one series share a cast: the FIRST dubbed episode's glossary
        # becomes the series "show bible" and later episodes just load it. That keeps
        # names/pronouns identical across episodes (each episode re-extracting risked
        # a different Hán-Việt spelling per episode) and drops an ~12s LLM call from
        # every episode after the first. Delete the series file to force a re-extract.
        series_path = _series_glossary_path(settings, context.series_id)
        reused = False
        if series_path is not None and series_path.exists():
            reused = True
            rows = json.loads(series_path.read_text(encoding="utf-8"))
            glossary = [GlossaryEntry(**row) for row in rows]
        else:
            glossary = extract_glossary(anchors, _translate_llm(settings), genre=context.config.content_genre, notes=context.config.content_notes)
            rows = [{"term_zh": g.term_zh, "term_vi": g.term_vi, "kind": g.kind, "note": g.note} for g in glossary]
            if series_path is not None and glossary:
                write_json(series_path, rows)

        context.artifacts["glossary"] = glossary
        tsv_path = settings.subtitles_dir / f"{context.job_id}.glossary.tsv"
        json_path = settings.subtitles_dir / f"{context.job_id}.glossary.json"
        write_tsv(tsv_path, [{k.upper(): v for k, v in row.items()} for row in rows], ["TERM_ZH", "TERM_VI", "KIND", "NOTE"])
        write_json(json_path, rows)
        return StageResult(
            name=self.name,
            artifacts=[_artifact(ArtifactKind.glossary, json_path, settings.data_dir)],
            metrics={"terms": len(glossary), "by_kind": _count_kinds(glossary), "reused_series_glossary": reused},
            logs=[format_glossary(glossary)[:2000]],
        )


def _series_glossary_path(settings: Settings, series_id: str | None) -> Path | None:
    if not series_id:
        return None
    return settings.subtitles_dir / f"series-{series_id}.glossary.json"


class TranslateStage(PipelineStage):
    name = StageName.translate

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        anchors: list[Anchor] = context.artifacts["anchors"]
        glossary: list[GlossaryEntry] = context.artifacts.get("glossary", [])
        chunks = build_chunks(anchors, flat_scene_map(anchors), ChunkConfig())
        speech_rows = translate(
            anchors, chunks, glossary, _translate_llm(settings),
            TranslateConfig(max_caption_chars=settings.subtitle_max_line_chars, voice_cps=settings.tts_voice_cps, notes=context.config.content_notes),
        )
        # Text-level pre-repair: character counts alone PREDICT which anchors will
        # overflow (read pace is known), so shorten them here in one concurrent
        # rewrite wave. Before this, ~half the anchors of a long video reached the
        # voiceover stage over budget and burned 3 synth-measure-repair rounds.
        speech_rows, pre_repaired = _shorten_predicted_overflow(speech_rows, anchors, glossary, settings)
        context.artifacts["chunks"] = chunks
        context.artifacts["speech_rows"] = speech_rows
        path = settings.subtitles_dir / f"{context.job_id}.speech.tsv"
        write_tsv(path, [r.to_row() for r in speech_rows], SPEECH_FIELDS)
        return StageResult(
            name=self.name,
            artifacts=[_artifact(ArtifactKind.subtitle_json, path, settings.data_dir)],
            metrics={"chunks": len(chunks), "speech_rows": len(speech_rows), "pre_repaired_anchors": pre_repaired},
        )


def _shorten_predicted_overflow(
    speech_rows: list[SpeechRow],
    anchors: list[Anchor],
    glossary: list[GlossaryEntry],
    settings: Settings,
) -> tuple[list[SpeechRow], int]:
    """Predict per-anchor voice duration from character counts and shorten the
    over-budget anchors via the normal repair path (batched, concurrent)."""
    effective_cps = settings.tts_voice_read_cps * settings.tts_synth_rate
    by_anchor: dict[int, int] = {}
    for row in speech_rows:
        by_anchor[row.anchor_id] = by_anchor.get(row.anchor_id, 0) + len(row.text)
    predicted_overflow: dict[int, tuple[float, float]] = {}
    for anchor in anchors:
        chars = by_anchor.get(anchor.id)
        if not chars:
            continue
        window = max(0.1, anchor.end - anchor.start)
        est_duration = chars / effective_cps
        # same tolerance as the render loop's repair_over_ms, plus the tempo fit's
        # ability to absorb a bit of overrun
        if est_duration > (window * settings.tts_max_tempo) + 0.3:
            predicted_overflow[anchor.id] = (window, est_duration)
    if not predicted_overflow:
        return speech_rows, 0
    return repair(
        speech_rows, anchors, glossary, predicted_overflow, _polish_llm(settings),
        RepairConfig(max_caption_chars=settings.subtitle_max_line_chars),
    )


class QAStage(PipelineStage):
    name = StageName.qa

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        anchors: list[Anchor] = context.artifacts["anchors"]
        speech_rows: list[SpeechRow] = context.artifacts["speech_rows"]
        cues = build_display_cues(speech_rows, settings.min_duration, anchors, context.artifacts.get("segments"), max_chars=settings.subtitle_max_line_chars)
        report = qa_report(speech_rows, anchors, cues, max_display_chars=settings.subtitle_max_line_chars)

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
        synth_client = _build_capcut_client(settings)
        render_config = RenderConfig(
            provider_url="capcut-native" if synth_client is not None else settings.tts_server_url,
            speaker=settings.tts_voice,
            text_mode=settings.tts_text_mode,
            min_tempo=settings.tts_min_tempo,
            max_tempo=settings.tts_max_tempo,
            fixed_tempo=settings.tts_fixed_tempo,
            glue_gap=settings.tts_glue_gap,
            crossfade=settings.tts_crossfade,
            min_duration=settings.min_duration,
            workers=settings.tts_workers,
            batch_size=settings.capcut_batch_size,
            batch_concurrency=settings.capcut_batch_concurrency,
            synth_rate=settings.tts_synth_rate,
        )
        work_dir = context.job_dir / "tts"
        cache_dir = settings.tts_cache_dir
        output_audio = settings.tts_dir / f"{context.job_id}.voice.m4a"
        segments = context.artifacts.get("segments")

        result = render(speech_rows, anchors, render_config, work_dir, cache_dir, segments=segments, synth_client=synth_client)
        logs = [f"render round 0: {result.summary}"]
        repair_cfg = RepairConfig(max_caption_chars=settings.subtitle_max_line_chars)
        rounds = 0
        while rounds < _VOICEOVER_REPAIR_ROUNDS:
            # Overflow: voice overruns the window -> shorten. Underfull: voice far
            # shorter than the source (often dropped content) -> complete it.
            overflow = {t.anchor_id: (t.target_duration, t.tts_duration) for t in result.timings if t.needs_repair}
            # Only restore lines that are clearly gutted (voice fills <40% of the
            # window) — leftover window at natural pace is an intended pause, not a
            # dropped idea, so we must not re-inflate it back into a rushed line.
            underfull = {
                t.anchor_id: (t.target_duration, t.tts_duration)
                for t in result.timings
                if t.target_duration >= 2.5 and t.tts_duration < t.target_duration * 0.4
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
            result = render(speech_rows, anchors, render_config, work_dir, cache_dir, segments=segments, synth_client=synth_client)
            logs.append(f"render round {rounds}: {result.summary}")

        # Assemble the final track once, after timings have converged.
        assemble_voice(result, output_audio, work_dir, glue_gap=render_config.glue_gap, crossfade=render_config.crossfade, pitch=settings.tts_pitch_shift)
        context.artifacts["speech_rows"] = speech_rows
        # Voice and subtitle are built from the SAME sentence units. Time each
        # sentence's caption within the REAL synthesized clip span (placement + voice
        # duration), so the caption matches the dub's actual timing — from the TTS.
        placements, _track_end = plan_placements(result.segments, render_config.glue_gap, render_config.crossfade)
        voice_windows = {
            round(seg_start, 3): (place, place + dur)
            for (seg_start, _clip, _play, _end), (place, _p, dur, _g) in zip(result.segments, placements)
        }
        cues = build_display_cues(speech_rows, settings.min_duration, anchors, segments, max_chars=settings.subtitle_max_line_chars, voice_windows=voice_windows, lead=settings.subtitle_lead)
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
            bed_volume=settings.dub_bed_volume,
            hardsub_config=_hardsub_config(settings),
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
        ArtifactKind.thumbnail: "image/jpeg",
    }.get(kind, "application/octet-stream")
    return Artifact(
        kind=kind,
        path=str(path.relative_to(data_dir)),
        mime_type=mime,
        size_bytes=path.stat().st_size if path.exists() else None,
    )


def _make_thumbnail(video: Path, out: Path) -> Path | None:
    """Grab a small poster frame from the video for the recent-jobs list. Best-effort:
    a thumbnail is cosmetic, so any ffmpeg hiccup just skips it."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=320:-2", str(out)],
        capture_output=True, text=True,
    )
    return out if completed.returncode == 0 and out.exists() else None


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
        "--workers", str(settings.asr_workers),
        "--max-chars", "18",
    ]
    return run_command(command, cwd=settings.root_dir)
