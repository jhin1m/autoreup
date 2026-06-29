# AutoReup Python Production Architecture

AutoReup prod v1 stays on Python/FastAPI because the expensive work is external (`yt-dlp`, `ffmpeg`, ASR worker, LLM gateway). The app code should focus on orchestration, state, deterministic subtitle quality, and UX.

This layout follows the common 2026 FastAPI layered pattern used by production templates: thin API routers, typed schemas, service/use-case layer, infrastructure adapters, and isolated pipeline stages.

## Directory layout

```text
backend/
  app/
    main.py                  # app factory, middleware, static mounts
    api/
      v1/endpoints/jobs.py   # HTTP/SSE job routes only
      v1/router.py           # versioned API composition
    core/
      config.py              # typed settings + paths
    domain/
      artifacts.py           # artifact domain model
      jobs.py                # job state/stage manifest domain model
    schemas/
      jobs.py                # request/response schemas
    services/
      job_service.py         # job create/read/run orchestration
      subtitle_quality.py    # deterministic QA + coverage
      subtitle_formats.py    # SRT/VTT helpers
      artifact_service.py    # artifact lookup/download paths
    pipeline/
      contracts.py           # stage interface/result
      production.py          # stage order + prod pipeline
    infrastructure/
      downloader.py          # yt-dlp adapter
      process.py             # subprocess runner
      packaging.py           # ffmpeg/zip output package
    integrations/
      llm/                   # provider-neutral LLM protocol
frontend/
  src/                       # React + @xyflow/react Studio SPA
  dist/                      # production static build served by FastAPI
scripts/
  asr_worker_client.py       # the only remaining external-service adapter script
data/
  jobs/<job_id>/manifest.json
```

## Boundaries

- `api` validates HTTP and returns schemas. No pipeline logic.
- `services` own use-cases and persistence.
- `pipeline` owns stage ordering and artifact contracts.
- `infrastructure` owns external process/tool details.
- `integrations.llm` owns provider-specific request formats.
- `dubbing` owns the in-process pipeline stages and the typed data contract.

## Production contract

Pipeline order stays deterministic:

```text
download -> asr -> glossary -> translate -> edit -> qa -> voiceover -> package
```

LLMs translate, edit, or shorten bounded anchor units only. Code owns the rail,
chunking, timing, coverage, QA, scheduling, and packaging.
