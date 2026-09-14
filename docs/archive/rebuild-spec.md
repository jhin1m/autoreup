# AutoReup v2 — Rebuild Spec & Plan

> Trạng thái: **đề xuất, chờ duyệt**. Đây là source-of-truth cho việc xây lại lõi dubbing.
> Quyết định đã chốt: (1) mục tiêu = **lồng tiếng đầy đủ**; (2) phạm vi = **đa thể loại, data-driven**; (3) cách làm = **spec trước, code sau**.

## 1. Bối cảnh — tại sao xây lại

Dự án hiện có 3 thế hệ pipeline chồng nhau:

| Gen | Bản chất | Trạng thái | Vấn đề |
| --- | --- | --- | --- |
| 1. Subtitle-only | chỉ phụ đề mềm | chạy e2e 1 lần | không lồng tiếng → không đạt mục tiêu |
| 2. master_vi | 1 câu Việt/span → suy ra sub+TTS | wired vào FastAPI, hay fail ở QA | sai kiến trúc (gộp anchor mất ranh giới thoại, overflow toàn cục, lexical hardcode) |
| 3. anchor-rail harness | ASR micro-segment = rail cứng | render 2 video thật xanh | thuật toán đúng nhưng code là probe-sprawl phụ thuộc blob `run_smooth_60s_probe.py` (2.593 dòng) |

**Quyết định:** lấy **hợp đồng Gen-3** làm spec sản phẩm, viết lại lõi sạch, giữ hạ tầng external đã chắc, vứt probe + Gen-1/Gen-2.

## 2. Mục tiêu sản phẩm

Biến video Trung → video Việt có:
- voice-over TTS tiếng Việt (giọng cố định CapCut `BV421_vivn_streaming`);
- phụ đề 1 dòng khớp nội dung với voice-over;
- timing/QA deterministic trước khi đóng gói;
- **không khóa cứng thể loại**: glossary/style trích tự động từ source mỗi video.

Nguyên tắc bất biến (từ `pipeline_reverse_analysis.md`):
- ASR micro-segment là **rail cứng**, không bao giờ gộp làm mất ranh giới thoại; anchor chỉ là khung thời gian.
- Overflow là **lỗi cục bộ trong 1 anchor**, không được đẩy lệch anchor sau (absolute scheduler).
- LLM chỉ viết/sửa text Việt theo source bounded; code sở hữu segmentation, timing, QA, scheduling.
- Display giữ dấu câu; TTS strip dấu câu riêng. Không feed display-sliced cue vào TTS.

## 3. Kiến trúc đích

### 3.1 Nguyên tắc
- Giữ khung FastAPI + hạ tầng external; **viết lại lõi pipeline in-process** (không subprocess + TSV-trên-đĩa).
- Mọi stage là hàm Python nhận/trả dataclass typed; TSV/JSON chỉ là dạng *persist artifact*, không phải interface giữa stage.
- Một thư viện chung duy nhất cho helper (hết copy-paste 4–7 lần).
- Mọi prompt nằm trong `prompts/`, không hardcode inline.

### 3.2 Layout
```
backend/app/
  integrations/llm/        # GIỮ NGUYÊN — multi-provider client (tài sản tốt nhất)
  services/tts/            # GIỮ — HttpTtsProvider
  dubbing/                 # MỚI — lõi sạch thay scripts/
    models.py              # dataclasses: AsrSegment, Anchor, Chunk, SpeechRow, RenderManifest...
    tsv_io.py              # read/write/clean — MỘT bản duy nhất
    weights.py             # vi_speech_weight, split_text_for_explicit_turns — MỘT bản
    rail.py                # asr_segments + anchors (port build_anchors)
    glossary.py            # trích glossary/style data-driven từ source (đa thể loại)
    chunking.py            # port chunk_by_scene.py (deterministic)
    translate.py           # stateful chunk + rolling memory + dub-tail (port adapt_chunk_stateful)
    editor.py              # semantic editor gated (port auto_polish)
    scheduler.py           # absolute voice schedule + breath-gap + energy-snap (mine từ probe)
    repair.py              # measured-TTS budget loop (port repair_overbudget)
    prompts/               # *.txt — tất cả prompt ở đây
  pipeline/production.py   # VIẾT LẠI — 8 stage gọi dubbing/ in-process
infra/
  asr_worker_client.py     # GIỮ
  synthesize_tts.py        # GIỮ (hoặc gói vào services/tts)
  calibrate_tts_voice.py   # GIỮ
worker-ai/                 # GIỮ NGUYÊN — Cloudflare ASR Worker
frontend/                  # GIỮ — React + @xyflow/react
```

### 3.3 Giữ / Vứt

**GIỮ (đã chắc):**
- `backend/app/integrations/llm/{client,protocol}.py`
- `worker-ai/src/index.ts` + `asr_worker_client.py`
- `synthesize_tts.py`, `calibrate_tts_voice.py`, `backend/app/services/tts`
- Khung FastAPI: job state machine, manifest, SSE, artifact contract, studio/providers, frontend

**VỨT (sau khi mine helper + thuật toán):**
- `run_smooth_60s_probe.py` — trích `scheduler`/energy-snap/breath-gap/atempo-fit + helper rồi xóa
- `run_dub_agent_harness.py`, mọi `probe_*`, `*_speech_tsv`, `build_probe_dashboard.py`
- Gen-2: `create_master_vi_script.py`, `adapt_tts_script.py`, `display_resegment_subtitles.py`, `diagnose_source_cues.py`, `rescore_subtitle_quality.py`, `extract_source_glossary.py`, `adjust_subtitle_timing.py`
- Gen-1: stage `compress_hard_lines`/`reflow_hard_spans`

## 4. Hợp đồng dữ liệu

Artifact trung tâm là `speech` (mỗi dòng = 1 cue đọc + hiển thị). `anchors` là rail thời gian+source.

```
asr_segments : (id, start, end, text_zh)                          # micro-beat rail CỨNG
anchors      : (id, start, end, source_ids[], source_zh)          # khung thời gian
chunks       : (id, start_anchor, end_anchor, start, end, scene_ids[], labels[])
speech       : (sub_id, anchor_id, start, end, source_ids[], text_vi)   # ARTIFACT TRUNG TÂM
glossary     : (term_zh, term_vi, kind, note)                     # data-driven mỗi video
render_manifest : per-anchor tts_duration, anchor_duration, overflow, needs_repair
```

Quy ước: `⟂` là marker tách explicit-turn trong text, truyền giữa editor/repair và splitter.

## 5. Pipeline 8 stage (in-process)

```
download → asr → build_rail → chunk → translate → edit → render+measure → repair(loop) → package
```

| Stage | Input | Output | Sở hữu | Fail mode |
| --- | --- | --- | --- | --- |
| download | url | video.mp4 + meta | yt-dlp adapter | lỗi network/cookie |
| asr | video | asr_segments + anchors | ASR worker | thiếu worker URL |
| build_rail | asr_segments | anchors (deterministic) | code | — |
| glossary | source_zh | glossary + style notes | LLM (1 pass, data-driven) | rỗng → fallback |
| chunk | anchors + scene | chunks | code thuần | — |
| translate | chunks + glossary + memory | speech.tsv + memory | LLM stateful | validation lỗi → trả lỗi rõ, KHÔNG break âm thầm |
| edit | speech + source | speech polished | LLM gated (severity + row-floor) | patch xấu bị filter |
| render+measure | speech + anchors | voice.m4a + render_manifest | scheduler + TTS + ffmpeg | overflow → repair |
| repair | speech + manifest | speech.repaired | LLM rút gọn theo weight | tối đa N vòng |
| package | video + speech + voice | dubbed.mp4 + srt/json + zip | ffmpeg | — |

Vòng lặp:
- **edit loop**: dừng khi `patched_anchors == 0`.
- **repair loop**: render `--timing-only` → đọc `anchors_need_repair` → rút gọn → render lại, tối đa N vòng; overflow không được push anchor sau (`overflow_pushes_next_voice=false`).

## 6. Đa thể loại (data-driven) — thay hardcode

Hiện Gen-2/Gen-3 hardcode bảng lexical Trung/Việt + glossary 1 video tu chân. Thay bằng:
- `glossary.py`: 1 LLM pass trích **term riêng/tên/style/tone** từ chính source mỗi video → `glossary` artifact. Không khóa thể loại trong prompt.
- `genre` chỉ là một **hint optional** (config), không phải điều kiện cứng. Prompt template tham số hóa theo glossary đã trích, không nhúng danh sách từ cố định.
- Bỏ mọi `normalize_vi_term`/`NO_BREAK_BIGRAMS`/`TURN_STARTERS` hardcode; ranh giới turn lấy từ `asr_segments` (rail) + marker `⟂` do LLM/editor đặt, không đoán bằng regex tiếng Việt.

## 7. Kế hoạch theo phase (mỗi phase có acceptance gate)

**Phase 0 — chốt nền**
- `git init`; mở rộng `.gitignore`: thêm `data/probes/`, `data/cache/`, `data/tts/cache*`, `data/jobs/`, `worker-ai/.wrangler/`.
- Giữ 2 video + 2 `speech.tsv` golden (Douyin `7647807375172209955`, `7649023281428778240`) làm regression target.
- Gate: repo init, `pytest` 60/60 vẫn xanh, working tree sạch.

**Phase 1 — thư viện chung**
- `dubbing/tsv_io.py`, `weights.py`, `models.py`, `prompts/` — gom mọi bản copy-paste về 1 nguồn, có unit test.
- Gate: test cho `vi_speech_weight`, `split_text_for_explicit_turns`, TSV roundtrip.

**Phase 2 — stage deterministic (không LLM)**
- Port `rail.py` + `chunking.py`.
- Gate: cùng input ASR → ra `anchors`/`chunks` khớp golden Jun-20 (anchor 6 giữ micro-segment 18–20).

**Phase 3 — stage LLM in-process**
- `glossary.py` (data-driven) → `translate.py` → `editor.py`; bỏ subprocess; validation trả lỗi rõ.
- Gate: dịch 1 chunk golden → speech.tsv: no Han, SUB_ID tuần tự, coverage đủ.

**Phase 4 — render + repair**
- Trích `scheduler.py` từ probe; dựng `repair.py` loop; ráp `production.py` mới (8 stage in-process).
- Gate: render 1 video golden → `anchors_need_repair=0`, `anchors_over_300ms=0`, `overflow_pushes_next_voice=false`, `display_equals_tts_chunks=true`.

**Phase 5 — regression + tích hợp**
- Chạy lại cả 2 video golden, so timing/structure phải xanh như Jun-20.
- Nối `production.py` mới vào FastAPI job runner + UI; cập nhật README/docs (sửa "Svelte"→React).
- Gate: 2 video qua e2e qua API; test contract pipeline xanh.

## 8. Rủi ro & xử lý
- **Mất chất lượng so với bản probe đã tinh chỉnh tay**: giữ golden render Jun-20 làm mốc so sánh khách quan ở mỗi phase.
- **Thuật toán scheduler/energy-snap khó tách khỏi blob 118k**: Phase 4 tách có chủ đích, viết test so output audio-timing trước/sau.
- **LLM prompt drift khi bỏ hardcode glossary**: data-driven glossary phải được kiểm bằng golden (tên riêng, cấp bậc tu luyện giữ đúng).

## 9. Việc KHÔNG làm trong v2
- Không hỗ trợ nhiều giọng TTS (giữ 1 giọng cố định).
- Không real-time/streaming render.
- Không quản lý multi-user; vẫn là local studio cá nhân.
```
