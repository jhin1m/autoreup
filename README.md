# AutoReup Studio

Local personal studio that turns a Chinese video into a Vietnamese-localized video:
one-line Vietnamese subtitles plus a Vietnamese voice-over, with the subtitles and
voice-over derived from the same edited script and gated by deterministic timing/QA.

The pipeline runs entirely in-process (FastAPI). External work is delegated to a
Cloudflare ASR worker, an OpenAI-compatible LLM gateway, a CapCut-style TTS server,
`yt-dlp`, and `ffmpeg`.

## Pipeline

```text
download -> asr -> glossary -> translate -> edit -> qa -> voiceover -> package
```

- **asr** — Chinese speech-to-text, then build the anchor rail (ASR micro-segments
  are the hard rail; anchors are timing windows over them).
- **glossary** — data-driven term/name extraction from the source (any genre; not
  hardcoded to one show).
- **translate** — stateful chunk translation with rolling memory into Vietnamese
  speech rows (the central `speech` artifact).
- **edit** — severity-gated semantic review with row-floor anti-collapse.
- **qa** — deterministic gate: no Han, full anchor coverage, sequential cue ids,
  no overlaps, one-line display ceiling.
- **voiceover** — TTS render with absolute scheduling (overflow stays local to one
  anchor) and a measured-TTS repair loop. Optional; skipped unless TTS is enabled.
- **package** — soft-sub MP4, dubbed MP4 (voice ducked over the original bed), SRT/VTT/JSON, ZIP.

The dubbing core lives in `backend/app/dubbing/`. Only one external-adapter script
remains under `scripts/` (`asr_worker_client.py`).

### Douyin downloads

yt-dlp can't sign Douyin's web API, so Douyin is downloaded by driving a headless
Chromium (Edge/Chrome, reused from the system — no bundled browser) over CDP via
`tools/douyin_extract.mjs` (Node, zero npm deps). It loads the page with your saved
cookies so Douyin's own JS yields the real CDN stream, which is then fetched directly.

- Requires **Node.js ≥ 22** and an installed Chromium browser (Edge/Chrome).
- Add your Douyin cookies in the UI (paste Cookie-Editor JSON / `cookies.txt` / header).
- Optional: `AUTOREUP_BROWSER_PATH` to point at a specific browser binary.

Everything else (Bilibili, etc.) still uses yt-dlp.

## Run local

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
uvicorn backend.app.main:app --reload
```

Open `http://127.0.0.1:8000`. The UI is a React + `@xyflow/react` SPA in
`frontend/src`. For UI development: `cd frontend && npm run dev`.

## Server-side config

End users only see video management. Provider/tool settings stay in environment variables:

```bash
AUTOREUP_ASR_WORKER_URL="https://..."
AUTOREUP_LLM_PROVIDER="responses"
AUTOREUP_LLM_BASE_URL="http://localhost:8090/v1"
AUTOREUP_TRANSLATE_MODEL="gpt-5.4-mini"
AUTOREUP_POLISH_MODEL="gpt-5.4"
AUTOREUP_COOKIES_FILE="/absolute/path/to/cookies.txt"
AUTOREUP_TTS_ENABLED=false
AUTOREUP_TTS_SERVER_URL="http://localhost:8080"
AUTOREUP_TTS_VOICE="BV421_vivn_streaming"
```

Provider cookies imported from the UI are stored outside the served media directory:

```text
.autoreup-private/cookies/
```

## API

- `GET /api/v1/providers/sessions` — Douyin/Bilibili cookie session status.
- `PUT|DELETE /api/v1/providers/{platform}/cookies` — save/clear local provider cookies.
- `GET /api/v1/studio/state` — source videos, download queue, assets.
- `POST /api/v1/studio/discover` — discover videos from a URL.
- `POST /api/v1/studio/download-queue` — queue selected discovered videos.
- `POST /api/v1/jobs` — create a dubbing job from a URL.
- `GET /api/v1/jobs` · `GET /api/v1/jobs/{id}` · `GET /api/v1/jobs/{id}/events` (SSE).
- `GET /api/v1/jobs/{id}/artifacts/{kind}` — download an artifact.

## Tests

```bash
. .venv/bin/activate && python -m pytest tests/ -q
```
