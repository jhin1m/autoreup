# AutoReup Studio — SPEC

> **Source of truth duy nhất.** Cập nhật: 2026-09-14.
> Mọi thay đổi contract (pipeline, artifact, API, UI, quality gates) sửa file này.
> Các doc trong `docs/archive/` chỉ là lịch sử — không dùng để code.

## 1. Sản phẩm

Local personal studio: **video Trung → video Việt hoá**.

- Phụ đề **1 dòng** tiếng Việt trên video.
- Voice-over TTS tiếng Việt (giọng CapCut cố định `BV421_vivn_streaming`), tuỳ chọn bật/tắt.
- Sub và voice-over **cùng một nguồn Việt** (`master_vi`) → nội dung khớp nhau.
- Timing/QA deterministic trước khi đóng gói.
- Đa thể loại, data-driven: glossary/style trích tự động từ source mỗi video, không hardcode.
- Single-user, chạy local. Không multi-tenant, không cloud deploy.

## 2. Bất biến kiến trúc

1. **ASR micro-segment là rail cứng** — không bao giờ gộp làm mất ranh giới thoại; anchor chỉ là khung thời gian.
2. **Một nguồn Việt cho một semantic span**: `SOURCE_ZH + context/glossary + timing → master_vi`; `master_vi → display 1-line` và `master_vi → TTS`. Không dịch subtitle và narration riêng.
3. **Code sở hữu**: segmentation, timing, QA, scheduling, packaging. **Agent (CLI headless) chỉ viết/sửa text bounded** (TSV `ID<TAB>TEXT`), không tự chọn segmentation.
4. **Overflow là lỗi cục bộ trong 1 anchor** (absolute scheduler) — không đẩy lệch anchor sau.
5. **Stage idempotent theo job dir**; job resume được từ manifest.

## 3. Pipeline (in-process, FastAPI)

```text
download → asr → glossary → translate → qa → voiceover (optional) → package
```

| Stage | Input | Output | Sở hữu | Fail mode |
| --- | --- | --- | --- | --- |
| download | url | video.mp4 + meta | yt-dlp adapter (Douyin: CDP extractor) | network/cookie |
| asr | video | asr_segments + anchors | Cloudflare ASR worker | thiếu worker URL |
| glossary | source_zh | glossary + style notes | agent 1 pass data-driven | rỗng → fallback |
| translate | chunks + glossary + memory | speech rows + memory | agent stateful, bounded | validation lỗi → fail rõ |
| qa | speech + rail | qa_report | code thuần | gate không đạt → fail |
| voiceover | speech + anchors | voice.m4a + render_manifest | scheduler + TTS + ffmpeg | overflow → repair loop ≤3 vòng |
| package | video + speech + voice | softsub/dubbed mp4, srt/vtt/json, zip | ffmpeg | — |

Ghi chú: stage `edit` đã **retired** (LLM editor pass rewrite ~nửa anchors, mỗi lần rewrite là một nguồn artifact mới). Chỉ giữ trong `StageName` để manifest job cũ còn parse.

## 4. Data contract (`backend/app/dubbing/`)

Artifact trung tâm là `speech`. `anchors` là rail thời gian + source.

```text
asr_segments : (id, start, end, text_zh)                        # micro-beat rail CỨNG
anchors      : (id, start, end, source_ids[], source_zh)        # khung thời gian
chunks       : (id, start_anchor, end_anchor, start, end, scene_ids[], labels[])
speech       : (sub_id, anchor_id, start, end, source_ids[], text_vi)   # ARTIFACT TRUNG TÂM
glossary     : (term_zh, term_vi, kind, note)                   # data-driven mỗi video
render_manifest : per-anchor tts_duration, anchor_duration, overflow, needs_repair
```

- `⟂` = marker tách explicit-turn, truyền giữa translate/repair và splitter.
- Display giữ dấu câu; TTS strip dấu câu riêng. **Không feed display-sliced cue vào TTS.**
- TSV/JSON chỉ là persist artifact — interface giữa các stage là dataclass typed.

## 5. Library (SQLite `data/library.db`)

Quyết định đã khoá: **SQLite** · **xoá series = cascade** · **ghép nối thẳng, không card chuyển tập** · **1 tập → 1 series**.

```sql
series(id, title, cover_url, tags json, created_at, updated_at)
series_source(id, series_id, platform, url, label, last_scanned_at)
episode(id, series_id, platform, platform_video_id, source_url, title,
        thumbnail_url, duration, uploader, discovered_at, dub_job_id, dub_state,
        UNIQUE(series_id, platform, platform_video_id))       -- dedup tập
export(id, series_id, title, description, hashtags json,
       episode_job_ids json ordered, output_path, duration, state, error,
       created_at, updated_at)
```

- `dub_state ∈ {none, translating, dubbed, failed}` (suy từ job state, cache vào episode).
- `export.state ∈ {draft, rendering, done, failed}`.
- JobManifest có `series_id`, `source_video_id`; job xong → cập nhật `episode.dub_state/dub_job_id`.
- Render export: chuẩn hoá từng tập (re-encode spec chung) rồi nối; output `data/exports/<series>/<export_id>.mp4`.

## 6. Jobs

```text
queued → running → succeeded | failed | canceled
```

- Manifest `data/jobs/<job_id>/manifest.json`: `schema_version`, `input`, `state`, `config`, `artifacts[]`, `stages[]` (status, timestamps, metrics, error).
- Artifact kinds: `video`, `source_json`, `subtitle_json/srt/vtt`, `qa_report`, `glossary`, `pipeline_audit`, `tts_manifest`, `voice_track`, `soft_sub_mp4`, `dubbed_mp4`, `hardsub_mp4`, `zip_package`, `thumbnail`.
- SSE: `GET /jobs/{id}/events`.

## 7. Quality gates (mặc định)

- `max_chars 60`, `max_cps 32.0`, `target_cps 22.0`, `min_duration 0.45s`, `max_duration 4.8s`.
- Display: **1 dòng**, `display_max_chars 32`, line width pro 40 ký tự, caption lead 0.12s trước voice.
- Không ký tự Hán trong output Việt.
- Không missing/duplicate source cue coverage; không overlap; gap sau snap ≤ 0.12s.

## 8. TTS contract

- Giọng cố định `BV421_vivn_streaming`; text mode `continuous`; speed/pitch/volume = 10 (neutral).
- Budget **12 chars/sec** (đo từ voice thật, không đoán); raw median ~14.16 cps.
- Accept nếu `0.92 ≤ tempo_needed ≤ 1.18` và không vi phạm semantic/glossary.
- Overflow → repair `master_vi` các ID lỗi → resynthesize **chỉ các ID đó**; tối đa 3 vòng.
- Package dubbed: giữ audio gốc làm bed, duck dưới voice Việt (sidechain) — **không mute** mặc định.

## 9. Agent runtime (headless CLI — không gọi LLM API)

Mọi bước text (glossary, translate, repair, naming) chạy qua **CLI agent headless**, không API key:

- **provider `agy`** (mặc định): Antigravity `agy -p <prompt> --output-format json --print-timeout <t> [+ --model, --effort]`;
  login subscription qua HOME — `agent_home` để tách account (mỗi account một HOME riêng).
- **provider `command`**: template CLI bất kỳ cho agent khác — ACP (`acpx claude exec --format quiet {prompt}`),
  `claude -p`, `codex exec`… `{prompt}` được thay thế, hoặc prompt append cuối argv nếu không có placeholder.
- Prompt gửi đi = `system + "\n\n---\n\n" + user`. Retry mặc định 3 lần backoff; lỗi trả `AgentError`.
- Adapter nằm ở `backend/app/agents/` (`runtime.py`, `agy.py`, `command.py`); stage modules chỉ gọi `complete(system, user, config)`.

## 10. API v1

```text
# Providers
GET    /providers/sessions                     # trạng thái cookie Douyin/Bilibili
PUT    /providers/{platform}/cookies           # lưu cookies (Cookie-Editor JSON / cookies.txt / header)
DELETE /providers/{platform}/cookies

# Studio (discovery)
GET    /studio/state                           # source videos + queue + assets
POST   /studio/discover  { url }
POST   /studio/download-queue
POST   /studio/add-video
POST   /studio/import-channel · /preview-channel
GET    /studio/thumb

# Jobs
POST   /jobs                                   # tạo job từ URL
GET    /jobs · GET /jobs/{id} · GET /jobs/{id}/events (SSE)
POST   /jobs/{id}/cancel
GET    /jobs/{id}/artifacts/{kind}             # tải artifact
DELETE /jobs/{id}

# Library / Series
GET    /series?q=&sort=&limit=&offset=         # {items, total}
POST   /series · GET/PATCH/DELETE /series/{id} # delete = cascade (bỏ purge)
POST   /series/{id}/sources · DELETE /series/{id}/sources/{sid}
POST   /series/{id}/scan                       # upsert episodes, dedup
GET    /series/{id}/episodes?dub_state=
POST   /series/{id}/translate  { episode_ids[] }
POST   /series/{id}/translate-titles

# Exports (ghép tập)
GET/POST /series/{id}/exports
GET/DELETE /exports/{export_id}
POST   /exports/{export_id}/render
POST   /series/{id}/suggest-export

# Media
GET    /media/...                              # file dưới data/
```

## 11. UI

Màn hình (React + Vite, serve bởi FastAPI):

- **Library** `/` — grid series + import URL/kênh.
- **Series detail** `/series/:id?tab=source|dubbed|export` — 3 tab: Nguồn / Đã dịch / Export; tab nằm trong URL.
- **Settings** (modal) — cookie/session + cấu hình cần thiết; provider/model để env.

UX rules (bắt buộc):

1. **Title-first** — hiển thị title video (từ `metrics.download.title`), không phải hostname.
2. **Chỉ file final** trong Export: final SRT/VTT, softsub MP4, dubbed MP4, ZIP; JSON chỉ dưới nhãn "Dữ liệu phụ đề".
3. **Stage timeline đủ trạng thái** pending/running/done; không show log thô, path, tên provider.
4. **4 states rõ ràng** cho library/series: empty / running / failed (có action tiếp theo) / done.
5. **Preview là surface chính**: player + subtitle list sync (auto-scroll, jump tới dòng).
6. Mobile: sidebar thành drawer; `aria-live` cho job status.

## 12. Config (env `AUTOREUP_*`, file `.env`)

```text
Agent:    AGENT_PROVIDER (agy|command), AGENT_BIN, AGENT_HOME, AGENT_COMMAND,
          AGENT_EFFORT, AGENT_TIMEOUT, AGENT_RETRIES, TRANSLATE_MODEL, POLISH_MODEL
Content:  CONTENT_GENRE (hint, optional), CONTENT_NOTES (mặc định per-video)
ASR:      ASR_WORKER_URL, ASR_MODEL, ASR_WORKERS
Download: COOKIES_FILE, BROWSER_PATH, NODE_BIN
TTS:      TTS_ENABLED, TTS_SERVER_URL, TTS_VOICE, TTS_NATIVE, CAPCUT_SESSION_FILE
Limits:   MAX_CHARS, DISPLAY_MAX_CHARS, DISPLAY_MAX_LINES, MAX_CPS, TARGET_CPS,
          MIN_DURATION, MAX_DURATION, SNAP_GAP, MAX_CONCURRENT_JOBS
```

Provider cookies nhập từ UI lưu tại `.autoreup-private/cookies/` (ngoài thư mục media được serve).

## 13. Dev & test

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
uvicorn backend.app.main:app --reload          # mở http://127.0.0.1:8000

.venv/bin/python -m pytest tests/ -q           # 62 tests, phải xanh
cd frontend && npm run dev                     # UI dev
```

## 14. Non-goals (v2)

- Không multi-voice; giữ 1 giọng cố định.
- Không realtime/streaming render.
- Không multi-user.
- Không provider/model config trong UI.
- Không dùng API key LLM — chỉ headless CLI agent.
- Không subtitle editing workflow trong UI (preview/review ổn trước đã).
