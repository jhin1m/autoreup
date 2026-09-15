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
from backend.app.dubbing.coverage import (
    CoverageConfig,
    dedupe_overlaps,
    fill_coverage_gaps,
    fuse_rails,
    speech_coverage,
    speech_seconds,
)
from backend.app.dubbing.normalize import normalize_segments
from backend.app.dubbing.units import build_units
from backend.app.dubbing.display import build_display_cues, write_subtitles
from backend.app.dubbing.glossary import extract_glossary, format_glossary
from backend.app.agents import AgentConfig
from backend.app.dubbing.onset import _speech_probs, refine_segments
from backend.app.dubbing.models import (
    ANCHOR_FIELDS,
    SEGMENT_FIELDS,
    SPEECH_FIELDS,
    Anchor,
    AsrSegment,
    GlossaryEntry,
    SpeechRow,
)
from backend.app.dubbing.qa import qa_report
from backend.app.dubbing.rail import segments_from_srt
from backend.app.dubbing.render import RenderConfig, assemble_voice, render
from backend.app.dubbing.repair import RepairConfig, expand, repair
from backend.app.dubbing.translate import TranslateConfig, translate
from backend.app.dubbing.tsv_io import read_tsv, write_json, write_tsv
from backend.app.infrastructure.downloader import YtDlpDownloader
from backend.app.infrastructure.packaging import package_outputs
from backend.app.infrastructure.process import run_command
from backend.app.pipeline.contracts import PipelineContext, PipelineStage, StageResult

_VOICEOVER_REPAIR_ROUNDS = 3  # converge; each round only re-synthesizes what changed


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


def _translate_agent(settings: Settings) -> AgentConfig:
    return _agent_config(settings, settings.translate_model)


def _polish_agent(settings: Settings) -> AgentConfig:
    return _agent_config(settings, settings.polish_model)


def _agent_config(settings: Settings, model: str) -> AgentConfig:
    return AgentConfig(
        provider=settings.agent_provider,
        bin=settings.agent_bin,
        home=settings.agent_home,
        command=settings.agent_command,
        model=model,
        effort=settings.agent_effort,
        timeout=settings.agent_timeout,
        retries=settings.agent_retries,
    )


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
        duck=settings.dub_duck,
        carve_db=settings.dub_carve_db,
        video_codec_args=tuple(video_encode_args(settings)),
        scale_filter=canonical_scale_filter(settings),
        sharpen=settings.video_sharpen,
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

    def hydrate(self, context: PipelineContext) -> bool:
        video = context.settings.videos_dir / f"{context.job_id}.mp4"
        if not (video.exists() and video.stat().st_size > 0):
            return False
        context.artifacts["video"] = video
        return True

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

    def hydrate(self, context: PipelineContext) -> bool:
        settings: Settings = context.settings
        segments_tsv = settings.subtitles_dir / f"{context.job_id}.segments.tsv"
        anchors_tsv = settings.subtitles_dir / f"{context.job_id}.anchors.tsv"
        if not (segments_tsv.exists() and anchors_tsv.exists()):
            return False
        try:
            segments = [AsrSegment.from_row(r) for r in read_tsv(segments_tsv)]
            anchors = [Anchor.from_row(r) for r in read_tsv(anchors_tsv)]
        except (ValueError, KeyError):
            return False
        if not segments or not anchors:
            return False
        context.artifacts["segments"] = segments
        context.artifacts["anchors"] = anchors
        return True

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        if not settings.asr_worker_url:
            raise RuntimeError("Missing ASR worker URL. Set AUTOREUP_ASR_WORKER_URL.")
        output_srt = settings.subtitles_dir / f"{context.job_id}.asr.zh.srt"
        logs = _run_asr(context, output_srt)

        segments = segments_from_srt(output_srt)
        # Whisper timestamps lead the real words on noisy content (music/SFX) and it
        # hallucinates lines inside music-only passages — the dub then speaks before
        # the on-screen character and phantom dialogue appears. Align the rail to
        # Silero-VAD speech regions (the WhisperX-style fix) before anything trusts it.
        total = len(segments)
        video = Path(context.artifacts["video"])
        from backend.app.dubbing.onset import _speech_probs

        packed = _speech_probs(video)
        context.artifacts["vad_probs"] = packed
        from backend.app.dubbing.onset import tighten_to_speech

        segments, tightened = tighten_to_speech(segments, packed)
        if tightened:
            logs.append(f"vad-tighten: dropped {tightened} music/hallucination segments ({len(segments)} of {total} left)")
        from backend.app.services.audio import probe_duration

        duration = probe_duration(video)

        # Burned-in subtitles ARE the script for reup content: OCR them first and let
        # ASR fill only the caption-less ranges (original-language scenes, songs).
        ocr_stats = {"ocr_segments": 0, "ocr_coverage": 0.0}
        if settings.asr_ocr_subs:
            from backend.app.dubbing.ocr_subs import detect_band, extract_ocr_segments, ocr_coverage

            try:
                band = detect_band(video)
                ocr_segments = extract_ocr_segments(video, band) if band else None
            except Exception as exc:  # noqa: BLE001 - OCR is optional; ASR stands alone
                ocr_segments, band = None, None
                logs.append(f"ocr-subs failed: {exc}")
            if ocr_segments and len(ocr_segments) >= 5:
                coverage = ocr_coverage(ocr_segments, duration)
                ocr_stats = {"ocr_segments": len(ocr_segments), "ocr_coverage": round(coverage, 3)}
                segments, fuse_stats = fuse_rails(ocr_segments, segments)
                logs.append(
                    f"ocr-subs: {len(ocr_segments)} captions ({coverage*100:.0f}% runtime); "
                    f"kept {fuse_stats['asr_kept']} ASR segments, dropped {fuse_stats['asr_overlapped']} overlapped"
                )

        segments, moved, dropped = refine_segments(segments, video, packed=packed)
        logs.append(f"vad-align: moved {moved} starts, dropped {dropped} hallucinated of {len(segments)} segments")

        segments, gap_stats = fill_coverage_gaps(
            video=video,
            segments=segments,
            duration=duration,
            worker_url=str(settings.asr_worker_url or ""),
            model=settings.asr_model,
            packed=packed,
            config=CoverageConfig(
                enabled=settings.asr_gap_fill,
                min_gap=settings.asr_gap_min_seconds,
                speech_floor=settings.asr_gap_speech_floor,
            ),
        )
        if gap_stats["gaps"]:
            logs.append(
                f"gap-fill: {gap_stats['filled']}/{gap_stats['gaps']} speech gaps re-ASR'd "
                f"({gap_stats['seconds']}s) -> +{gap_stats['recovered']} segments, skipped {gap_stats['skipped']} lyric/artifact, "
                f"unrecovered {gap_stats['unrecovered']}s"
            )
        before_dedupe = len(segments)
        segments = dedupe_overlaps(segments)
        if len(segments) != before_dedupe:
            logs.append(f"rail-dedupe: {before_dedupe} -> {len(segments)} segments after overlap trim")
        segments, normalize_metrics = normalize_segments(segments, _translate_agent(settings))
        logs.append(f"normalize: patched {normalize_metrics['patched']} of {normalize_metrics['suspicious']} suspicious segments")
        covered, speech_total, ratio = speech_coverage([(s.start, s.end) for s in segments], packed)
        if speech_total > 0:
            logs.append(f"coverage: {covered:.0f}/{speech_total:.0f}s of speech texted ({ratio*100:.0f}%)")
            if ratio < 0.9:
                logs.append("coverage WARNING: unmatched speech remains")
        units = build_units(segments)
        anchors = [Anchor(id=u.id, start=u.start, end=u.end, source_ids=u.segment_ids, source_zh=u.source_zh) for u in units]
        context.artifacts["segments"] = segments
        context.artifacts["anchors"] = anchors

        segments_tsv = settings.subtitles_dir / f"{context.job_id}.segments.tsv"
        anchors_tsv = settings.subtitles_dir / f"{context.job_id}.anchors.tsv"
        write_tsv(segments_tsv, [s.to_row() for s in segments], SEGMENT_FIELDS)
        write_tsv(anchors_tsv, [a.to_row() for a in anchors], ANCHOR_FIELDS)
        return StageResult(
            name=self.name,
            artifacts=[_artifact(ArtifactKind.source_json, anchors_tsv, settings.data_dir)],
            metrics={"segments": len(segments), "units": len(anchors), "speech_coverage": round(ratio, 3), **normalize_metrics, **gap_stats, **ocr_stats},
            logs=logs,
        )


class GlossaryStage(PipelineStage):
    name = StageName.glossary

    def hydrate(self, context: PipelineContext) -> bool:
        json_path = context.settings.subtitles_dir / f"{context.job_id}.glossary.json"
        if not json_path.exists():
            return False
        try:
            rows = json.loads(json_path.read_text(encoding="utf-8"))
            context.artifacts["glossary"] = [GlossaryEntry(**row) for row in rows]
        except (json.JSONDecodeError, TypeError, OSError):
            return False
        return True  # an empty glossary is a valid extraction result

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
            glossary = extract_glossary(anchors, _translate_agent(settings), genre=context.config.content_genre, notes=context.config.content_notes)
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

    def hydrate(self, context: PipelineContext) -> bool:
        path = context.settings.subtitles_dir / f"{context.job_id}.speech.tsv"
        if not path.exists():
            return False
        try:
            rows = [SpeechRow.from_row(r) for r in read_tsv(path)]
        except (ValueError, KeyError):
            return False
        if not rows:
            return False
        context.artifacts["speech_rows"] = rows
        return True

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        anchors: list[Anchor] = context.artifacts["anchors"]
        segments: list[AsrSegment] = context.artifacts["segments"]
        glossary: list[GlossaryEntry] = context.artifacts.get("glossary", [])
        speech_rows = translate(
            anchors, segments, glossary, _translate_agent(settings),
            TranslateConfig(notes=context.config.content_notes, voice_cps=settings.tts_voice_cps),
        )
        context.artifacts["speech_rows"] = speech_rows
        path = settings.subtitles_dir / f"{context.job_id}.speech.tsv"
        write_tsv(path, [r.to_row() for r in speech_rows], SPEECH_FIELDS)
        return StageResult(
            name=self.name,
            artifacts=[_artifact(ArtifactKind.subtitle_json, path, settings.data_dir)],
            metrics={"speech_rows": len(speech_rows), "captions": len(segments)},
        )


class QAStage(PipelineStage):
    name = StageName.qa

    def hydrate(self, context: PipelineContext) -> bool:
        settings: Settings = context.settings
        subtitle_json = settings.subtitles_dir / f"{context.job_id}.vi.display.json"
        subtitle_srt = settings.subtitles_dir / f"{context.job_id}.vi.display.srt"
        if not (subtitle_json.exists() and subtitle_srt.exists()):
            return False
        try:
            cues = json.loads(subtitle_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        context.artifacts["display_cues"] = cues
        context.artifacts["subtitle_json"] = subtitle_json
        context.artifacts["subtitle_srt"] = subtitle_srt
        return True

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        segments: list[AsrSegment] = context.artifacts["segments"]
        speech_rows: list[SpeechRow] = context.artifacts["speech_rows"]
        cues = build_display_cues(
            speech_rows, segments, settings.min_duration,
            max_chars=settings.subtitle_max_line_chars, lead=settings.subtitle_lead,
        )
        report = qa_report(speech_rows, segments, cues, max_display_chars=settings.subtitle_max_line_chars)

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

    def hydrate(self, context: PipelineContext) -> bool:
        settings: Settings = context.settings
        if not settings.tts_enabled:
            return True  # stage would have been a no-op anyway
        voice = settings.tts_dir / f"{context.job_id}.voice.m4a"
        if not (voice.exists() and voice.stat().st_size > 0):
            return False
        context.artifacts["voice_track"] = voice
        return True

    def run(self, context: PipelineContext) -> StageResult:
        settings: Settings = context.settings
        if not settings.tts_enabled:
            return StageResult(name=self.name, metrics={"enabled": False}, logs=["TTS disabled; set AUTOREUP_TTS_ENABLED=true"])
        if not settings.tts_server_url:
            raise RuntimeError("Missing TTS server URL. Set AUTOREUP_TTS_SERVER_URL.")

        glossary: list[GlossaryEntry] = context.artifacts.get("glossary", [])
        speech_rows: list[SpeechRow] = context.artifacts["speech_rows"]
        segments: list[AsrSegment] = context.artifacts.get("segments", [])
        sources = {s.id: s.text_zh for s in segments}
        synth_client = _build_capcut_client(settings)
        render_config = RenderConfig(
            provider_url="capcut-native" if synth_client is not None else settings.tts_server_url,
            speaker=settings.tts_voice,
            text_mode=settings.tts_text_mode,
            crossfade=settings.tts_crossfade,
            workers=settings.tts_workers,
            batch_size=settings.capcut_batch_size,
            batch_concurrency=settings.capcut_batch_concurrency,
            synth_rate=settings.tts_synth_rate,
            lead=settings.tts_lead,
            tempo_ceiling=settings.tts_tempo_ceiling,
            overflow_tolerance=settings.tts_overflow_tolerance,
        )
        work_dir = context.job_dir / "tts"
        cache_dir = settings.tts_cache_dir
        output_audio = settings.tts_dir / f"{context.job_id}.voice.m4a"

        # ONE budget, measured and consistent with what the translator was told: the
        # caption's own window. The VAD speech is evidence only — it distinguishes a
        # genuine source pause (keep the dub short) from dropped content (complete it).
        packed = context.artifacts.get("vad_probs")
        if packed is None:
            packed = _speech_probs(Path(context.artifacts["video"]))
        window_of: dict[int, float] = {}
        vad_of: dict[int, float] = {}
        for index, row in enumerate(speech_rows):
            limit = speech_rows[index + 1].anchor_start if index + 1 < len(speech_rows) else row.anchor_end + 0.5
            window_of[row.sub_id] = round(max(0.1, row.anchor_end - row.anchor_start), 3)
            vad_of[row.sub_id] = round(speech_seconds(packed, row.anchor_start - 0.2, max(limit, row.anchor_end)), 3)

        result = render(speech_rows, render_config, work_dir, cache_dir, synth_client=synth_client)
        logs = [f"render round 0: {result.summary}"]
        repair_cfg = RepairConfig()
        rounds = 0
        while rounds < _VOICEOVER_REPAIR_ROUNDS:
            overflow = {
                t.sub_id: (window_of.get(t.sub_id, 0.0), t.tts_duration)
                for t in result.timings
                if t.tts_duration > window_of.get(t.sub_id, 0.0) + render_config.overflow_tolerance
            }
            # Underfill = the dub is far shorter than the caption while the SOURCE was
            # actually speaking through it (a real pause is not dropped content).
            underfull = {
                t.sub_id: (window_of.get(t.sub_id, 0.0), t.tts_duration)
                for t in result.timings
                if window_of.get(t.sub_id, 0.0) >= 1.0
                and t.tts_duration < window_of[t.sub_id] * settings.tts_expand_floor
                and vad_of.get(t.sub_id, 0.0) >= window_of[t.sub_id] * 0.7
            }
            if not overflow and not underfull:
                break
            changed = 0
            if overflow:
                speech_rows, n = repair(speech_rows, sources, glossary, overflow, _polish_agent(settings), repair_cfg)
                changed += n
                logs.append(f"round {rounds + 1}: shortened {n} over-budget rows")
            if underfull:
                speech_rows, n = expand(speech_rows, sources, glossary, underfull, _polish_agent(settings), repair_cfg)
                changed += n
                logs.append(f"round {rounds + 1}: completed {n} under-filled rows")
            rounds += 1
            if not changed:
                break
            result = render(speech_rows, render_config, work_dir, cache_dir, synth_client=synth_client)
            logs.append(f"render round {rounds}: {result.summary}")

        # Assemble the final track once, after timings have converged. The display
        # subtitles were already written by the QA stage from the same rows — their
        # windows are the captions', so voice timing never rewrites them.
        assemble_voice(result, output_audio, work_dir, crossfade=render_config.crossfade, pitch=settings.tts_pitch_shift)
        context.artifacts["speech_rows"] = speech_rows
        context.artifacts["timings"] = result.timings

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
        duck=settings.dub_duck,
        carve_db=settings.dub_carve_db,
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
        # Signed web API first (in-process, no browser); fall back to the headless
        # browser only when the API is risk-controlled (empty response).
        from backend.app.infrastructure.douyin.extractor import DouyinApiExtractor
        from backend.app.infrastructure.douyin.api_client import DouyinApiError, DouyinDrmError

        try:
            return DouyinApiExtractor(settings.videos_dir, vault).download(url, job_id)
        except DouyinDrmError:
            raise  # encrypted playlet — the browser sniffs the same DRM stream, no point
        except DouyinApiError:
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
        "--language", "auto",  # dialogue may be Chinese OR Japanese (mixed anime audio)
        "--model", str(settings.asr_model),
        "--workers", str(settings.asr_workers),
        "--chunk-seconds", str(settings.asr_chunk_seconds),
        "--max-chars", "18",
    ]
    return run_command(command, cwd=settings.root_dir)
