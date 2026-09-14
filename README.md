# AutoReup Studio

Local personal studio: **video Trung → video Việt hoá** — phụ đề 1 dòng + voice-over TTS tiếng Việt,
cùng một nguồn `master_vi` và gated bằng QA/timing deterministic trước khi đóng gói.

Pipeline in-process (FastAPI): `download → asr → glossary → translate → qa → voiceover → package`.
External tools: Cloudflare ASR worker, **headless CLI agent** (Antigravity `agy` / ACP — không API key),
CapCut-style TTS server, `yt-dlp`, `ffmpeg`. Douyin tải qua headless Chromium (CDP, `tools/douyin_extract.mjs`, Node ≥ 22).

> **Đọc [`docs/SPEC.md`](docs/SPEC.md) trước khi code.** Đó là source of truth duy nhất cho
> pipeline, artifact, library, API, UI và quality gates. `docs/archive/` chỉ là lịch sử.

## Run local

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cd frontend && npm install && npm run build && cd ..
uvicorn backend.app.main:app --reload
```

Mở `http://127.0.0.1:8000`. UI dev: `cd frontend && npm run dev`.

## Config

Provider/tool settings ở env (`AUTOREUP_*`, có thể đặt trong `.env`) — không hiện trong UI:

```bash
AUTOREUP_ASR_WORKER_URL="https://..."
AUTOREUP_AGENT_PROVIDER="agy"          # agy | command
AUTOREUP_AGENT_BIN="agy"
# AUTOREUP_AGENT_HOME=~/.agy-accounts/acc2        # account isolation (optional)
# AUTOREUP_AGENT_COMMAND="acpx claude exec --format quiet {prompt}"   # provider=command
AUTOREUP_TRANSLATE_MODEL="gemini-3.8-flash-low"
AUTOREUP_POLISH_MODEL="gemini-3.1-pro-high"
AUTOREUP_COOKIES_FILE="/absolute/path/to/cookies.txt"
AUTOREUP_TTS_ENABLED=false
AUTOREUP_TTS_SERVER_URL="http://localhost:8080"
AUTOREUP_TTS_VOICE="BV421_vivn_streaming"
```

Danh sách đầy đủ: [`docs/SPEC.md` §12](docs/SPEC.md). Provider cookies nhập từ UI lưu tại
`.autoreup-private/cookies/` (ngoài media directory được serve).

## API (tóm tắt)

- `GET /api/v1/providers/sessions` · `PUT|DELETE /api/v1/providers/{platform}/cookies`
- `GET /api/v1/studio/state` · `POST /api/v1/studio/discover` · `POST /api/v1/studio/download-queue`
- `POST /api/v1/jobs` · `GET /api/v1/jobs/{id}` · `GET /api/v1/jobs/{id}/events` (SSE) ·
  `GET /api/v1/jobs/{id}/artifacts/{kind}`
- `/api/v1/series…` (library + scan + translate) · `/api/v1/exports…` (ghép tập)

Đầy đủ: [`docs/SPEC.md` §10](docs/SPEC.md).

## Tests

```bash
. .venv/bin/activate && python -m pytest tests/ -q     # 62 tests
```
