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
download → asr ─┬ normalize ASR noise (rule + gated agent pass)
               └ build semantic units (L2)
         → glossary → translate (1 batch/call, rolling context)
                    → edit (1 gated creative pass per batch)
         → qa → voiceover (optional) → package
```

| Stage | Input | Output | Sở hữu | Fail mode |
| --- | --- | --- | --- | --- |
| download | url | video.mp4 + meta | yt-dlp adapter (Douyin: CDP extractor) | network/cookie |
| asr | video | asr_segments → units (sentence-level) | ASR worker + normalize + units builder | thiếu worker URL |
| glossary | source_zh | glossary + style notes | agent 1 pass data-driven | rỗng → fallback |
| translate | units + glossary + rolling context | speech rows (1/dòng lượt) | agent (no char budget) | validation lỗi → fail rõ |
| edit | draft VI + source + glossary | edited VI (fact-gated, max 1 pass) | agent | patch vi phạm → giữ draft |
| qa | speech + rail | qa_report | code thuần | gate không đạt → fail |
| voiceover | speech + anchors | voice.m4a + render_manifest | scheduler + TTS + ffmpeg | overflow → repair loop ≤3 vòng |
| package | video + speech + voice | softsub/dubbed mp4, srt/vtt/json, zip | ffmpeg | — |

Ghi chú: stage `edit` đã **retired** (LLM editor pass rewrite ~nửa anchors, mỗi lần rewrite là một nguồn artifact mới). Chỉ giữ trong `StageName` để manifest job cũ còn parse.

## 4. Data contract (`backend/app/dubbing/`)

Artifact trung tâm là `speech`. `unit` là đơn vị dịch (tên artifact cũ `anchors.tsv` giữ nguyên nhưng nội dung là unit).

```text
asr_segments : (id, start, end, text_zh)                        # micro-beat rail CỨNG
unit         : (id, segment_ids, start, end, source_zh, flags)  # câu/đoạn nghĩa — đơn vị dịch
run          : (id, unit_ids, start, end)                       # đoạn thoại liên tục (gap < 0.8s)
speech       : (sub_id, unit_id, start, end, source_ids, text_vi)  # 1 dòng / lượt thoại
glossary     : (term_zh, term_vi, kind, note + evidence)        # data-driven mỗi video
render_manifest : per-run fill/overflow, per-unit tts_duration
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
- Display: **1 dòng**, reflow width 40 ký tự; cue được phép vượt tối đa `LINE_SLACK = 8` ký tự để tránh ngắt xấu — **QA gate dùng đúng ngưỡng này** (reflow và gate cùng một nguồn).
- Không ký tự Hán trong output Việt.
- Không missing/duplicate source cue coverage; không overlap; gap sau snap ≤ 0.12s.
- Rail ASR: segment được co về đúng vùng VAD speech chứa nó (bỏ blob nhạc dài), hallucination nhạc bị loại; rail bất biến **sorted, không chồng lấn** sau gap-fill.
- **Nghĩa là đơn vị dịch (F1)**: `translate` là **một workspace agent run** — workspace `data/jobs/<job>/translate/` gồm `SKILL.md` + `input/{units,glossary,notes}`, agent đọc toàn bộ transcript, ghi `out/draft.tsv`, tự soát nghĩa rồi ghi `out/final.tsv`. Pipeline **đọc artifact từ đĩa** (không parse prose), và **gate code** mọi dòng đổi giữa draft→final (số/phủ định/glossary/độ dài) — không bao giờ đặt ngân sách độ dài trong prompt.
- Agent flags (agy): `--add-dir <ws> --dangerously-skip-permissions --sandbox`; timeout workspace phải phủ vòng lặp nội bộ của agent (`translate_timeout`).
- **Row = unit = clip**: mỗi unit một clip TTS neo vào cửa sổ của unit; timing không nội suy, không pack theo run, không clamp/hold.
- **Sub = voice, cùng thời điểm (F4)**: khối cue của unit = `[voice_start − lead, max(voice_end, unit_end)]` (clamp theo cue kế) — sub hiện trước voice ~0.12s và phủ trọn cửa sổ caption gốc; text sub dựng lại SAU fit loop nên luôn khớp đúng lời đọc.
- QA gate: mọi unit có dòng Việt đúng cửa sổ rail (`units_missing_coverage`, `rows_misaligned_with_rail`), không Hán, cue không chồng, 1 dòng/cue.

## 8. TTS contract

Voice tuning (BV421 'Nguồn nhỏ ngọt ngào', đã A/B chọn):
- text mode **`expressive`**: giữ sắc thái dấu câu (`,` = lấy hơi, `.` = xuống giọng, `…` = ngắt kịch tính, `! ?` = lên giọng) — gom mọi dấu thành một dấu phẩy nghe máy móc;
- `tts_synth_rate 1.0` — không time-stretch post-synth (artifact atempo đọc như robot); độ dài do text quyết;
- trim silence: keep 0.08s, ngưỡng -50dB (cắt sát làm cụt hơi = "khựng");
- mastering: compressor chậm (ratio 1.6, attack 20ms, release 240ms, makeup 1.2), presence 3.2k +1.2, air 10k +1.0, lowpass 15k;
- mix với âm gốc: bed **0.55**, **sidechain duck 6:1** (attack 25ms/release 420ms), **carve 2.5kHz −3dB** — nhạc/SFX nở lại khi ngừng thoại, tiếng gốc chỉ bị hạ lúc có thoại;
- timeline mỗi unit neo vào cửa sổ của nó: `start = max(voice trước kết thúc, unit.start - lead)`; voice chỉ được **tăng tốc** (≤ `tempo_ceiling 1.18`), chỉ vừa đủ để khớp, không bao giờ làm chậm; không thêm gap giữa clip (crossfade lo click).
- **Overlay (che + vị trí sub) là dữ liệu của job, ở không gian OUTPUT**: `data/jobs/<job>/overlay.json` (phân số 0..1) gồm các vùng che (blur/darken/solid + cường độ) và vị trí/cỡ sub; render scale/pad về khung canonical TRƯỚC rồi mới che + đặt sub — nên preview trong UI (đúng khung output, có thanh tua) luôn trùng bản burn; không có overlay thì dùng band dò tự động (Vision trước, heuristic pixel là fallback).
- fit loop chấm bằng SỐ ĐO: `voice > free_time (tới hình kế) + 0.4s` → rút gọn; `voice < 0.65 × VAD speech` **và** nguồn đang nói (`vad ≥ 0.8s`) → hoàn thiện; lặp tới hội tụ (≤3 vòng). Không dùng ngân sách ký tự để đoán.



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
