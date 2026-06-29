# AutoReup Production Pipeline Spec

This freezes the proven Python prototype as the product contract for the production FastAPI app.

## Golden regression target

- Video: `data/videos/10415b5732fa.mp4`
- Final subtitle JSON: `data/subtitles/10415b5732fa.asr.vi.fast6v2final.clean.json`
- Final subtitle SRT: `data/subtitles/10415b5732fa.asr.vi.fast6v2final.clean.srt`
- Final QA: `data/subtitles/10415b5732fa.asr.vi.fast6v2final.clean.qa.json`

Expected QA summary:

```json
{
  "total_cues": 724,
  "issues": 0,
  "avg_chars": 45.59,
  "avg_cps": 21.83,
  "max_chars": 60,
  "max_cps": 31.51
}
```

Coverage/timing gate for the same artifact:

```json
{
  "missing": 0,
  "duplicates": 0,
  "overlaps": 0,
  "tiny_gaps": 0,
  "contains_chinese": 0
}
```

## Product pipeline

1. Download video.
2. Chinese STT/ASR.
3. Normalize Chinese ASR with a safety guard.
4. Translate short source units.
5. Compress only hard subtitle lines (`too_long`, `too_fast`, duration, or Chinese leakage).
6. Deterministic hard-span reflow: code owns splits; the model only translates/narrows bounded source units.
7. Timing cleanup.
8. QA and package outputs.

The production system must not let an LLM freely choose subtitle segmentation. Raw-cue/source-unit clustering was tested and regressed coverage, merging, and transliteration quality.

## Runtime architecture

```text
UI/API
  -> job create(url)
  -> pipeline runner
       download -> asr -> normalize -> translate -> compress -> reflow -> timing -> qa -> package
  -> artifact manifest
  -> preview/download
```

Python/FastAPI owns app logic, job orchestration, artifact contracts, deterministic subtitle QA/timing/reflow, API state, and packaging. External media tools remain process dependencies: `ffmpeg` for remux/package and `yt-dlp` for download.

## Job state machine

```text
queued -> running -> succeeded
        \          -> failed
         \         -> canceled
```

Each stage records `pending/running/succeeded/failed/skipped`, start/end timestamps, output artifacts, metrics, and error text. Jobs are resumable from the artifact manifest; stages should be idempotent for the same job directory.

## Artifact manifest

Write one manifest per job, for example `data/jobs/<job_id>/manifest.json`:

```json
{
  "schema_version": "2026-06-18",
  "job_id": "10415b5732fa",
  "input": { "url": "..." },
  "state": "succeeded",
  "config": { "max_chars": 60, "max_cps": 32.0, "min_duration": 0.45, "max_duration": 4.8 },
  "artifacts": [
    { "kind": "video", "path": "videos/10415b5732fa.mp4", "mime_type": "video/mp4" },
    { "kind": "subtitle_json", "path": "subtitles/10415b5732fa.vi.json", "mime_type": "application/json" },
    { "kind": "subtitle_srt", "path": "subtitles/10415b5732fa.vi.srt", "mime_type": "application/x-subrip" },
    { "kind": "subtitle_vtt", "path": "subtitles/10415b5732fa.vi.vtt", "mime_type": "text/vtt" },
    { "kind": "qa_report", "path": "subtitles/10415b5732fa.qa.json", "mime_type": "application/json" },
    { "kind": "soft_sub_mp4", "path": "packages/10415b5732fa.softsub.mp4", "mime_type": "video/mp4" },
    { "kind": "zip_package", "path": "packages/10415b5732fa.zip", "mime_type": "application/zip" }
  ],
  "stages": []
}
```

## UI/API contract

- `POST /api/v1/jobs` with URL creates a job.
- `GET /api/v1/jobs` lists recent video jobs.
- `GET /api/v1/jobs/{id}` returns state, current stage, progress, errors, metrics, and artifact links.
- `GET /api/v1/jobs/{id}/events` streams progress updates.
- `GET /api/v1/jobs/{id}/artifacts/{kind}` downloads an artifact.

UI flow:

1. Video library, URL import, and job status management.
2. Progress timeline with user-facing stage labels.
3. Preview original MP4 with JSON/VTT soft subtitles; no rerender for preview.
4. Download SRT, VTT, JSON, soft-sub MP4 (`mov_text` remux), or ZIP package.

Provider/model/cookie/worker settings are server-side environment config, not end-user UI fields.

## Default quality gates

- `max_chars`: 60
- `max_cps`: 32.0
- `min_duration`: 0.45 seconds
- `max_duration`: 4.8 seconds
- No Chinese characters in Vietnamese output.
- No missing or duplicated source cue coverage.
- No overlaps and no gaps in `(0, 0.12]` after snap cleanup.

LLM defaults should target the local OpenAI-compatible Responses API first:

- Base URL: `http://localhost:8090/v1`
- `stream=true`
- `reasoning.effort=none`
