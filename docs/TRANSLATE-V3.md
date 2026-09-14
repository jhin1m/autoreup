# TRANSLATE-V3 — blueprint

> Rewrite blueprint cho tầng dịch + timeline. Baseline đo bằng
> `scripts/audit_dub_timing.py` trên 10 job thật (2026-09-14).
> Nguyên tắc: **nghĩa do agent, thời gian do code**; bỏ legacy sau khi đạt target.

## Baseline (10 job)

| Metric | Giá trị |
|---|---|
| voice/source ratio | 0.84 (dub ngắn hơn source 16%) |
| Im lặng trong speech run | 16.25% thời lượng voice (worst 25.6%) |
| Pause ≥300ms / tập | ~85 |
| Anchor hụt >25% | 20% (126/626) — `expand` cũ bắt 0 case |
| rows/anchor | 2.62 |
| Rows cụt giữa câu | 28.4% |
| Display cues vượt max chars | 205 / 10 job |
| QA issues | 0 |

## Kiến trúc

```
L0 rail      asr_segments (bất biến, giữ)
L1 normalize rules + agent fallback (chỉ câu bị cờ)          [mới]
L2 units/runs unit = câu/đoạn nghĩa; run = gap < 0.8s        [mới]
L3 glossary  evidence bắt buộc, 1 term_zh → 1 term_vi        [sửa]
L4 translate dịch theo unit, KHÔNG budget chữ trong prompt   [viết lại]
L5 reflow    unit_vi → câu → display cue ≤ max chars         [mới]
L6 timeline  run-level: tempo + breath + lead budget         [mới]
L7 fit       overflow/underfill đo theo RUN, chỉ sửa vi phạm [gộp]
L8 qa        thêm gate cps/chars/⟂/intra-run silence         [mở rộng]
L9 voiceover/package (giữ, thay planner)
```

## L6 — quy tắc tiêu thụ slack (phần chữa "đứt quãng")

Với mỗi run: `span = run.end - run.start`, `voice = Σ clip`.

1. **Tempo run-level**: `tempo = clamp(voice / span, 0.92, 1.0)` khi dub ngắn —
   một hệ số chung cả run (không giãn clip lẻ gây lurch).
2. **Breath phân bổ**: slack còn lại chia tại ranh giới câu, mỗi cái `[0.06, 0.25]s`.
3. **Lead budget**: trong run, voice được trôi sớm hơn hình tối đa `max_lead` (mặc định
   0.8s cho thuyết minh; 0.3s nếu cần khớp miệng) — đây là điều kiện để hết khựng.
4. **Overflow**: voice dài hơn span → speed-up ≤1.18, phần dư tràn vào pause sau,
   **không bao giờ đẩy run kế**.
5. **Underfill**: sau 1+2 mà `run_fill < 0.75` → flag cho L7 EXPAND (chỉ những run đó).

## Contract

```text
Unit      : id, segment_ids, start, end, source_zh, flags[]
Run       : id, unit_ids, start, end
Placement : unit_id, place_start, playback_duration, tempo, breath_before,
            early_lead, run_overflow
```

## Target nghiệm thu (đo lại bằng audit script)

| Metric | Baseline | Target |
|---|---|---|
| Im lặng trong run | 16.25% | < 5% |
| Pause ≥300ms/tập | ~85 | < 25 |
| rows/anchor | 2.62 | ≤ 1.2 |
| Rows cụt | 28.4% | < 10% |
| Overlong cues | ~20/tập | 0 |
| QA issues | 0 | 0 |
| Early lead | ≤0.3s | ≤ lead budget |

## Kết quả mô phỏng L6 (10 job, `scripts/simulate_timeline.py`)

Cùng dữ liệu thật (anchors + TTS durations đo được), so 2 planner:

| Planner | Khoảng khựng (>0.3s giữa câu trong run) |
|---|---|
Cũ (per-anchor, max_lead 0.3) | **468.9s** tổng, 11–63 gap/job |
V3 chain + breath (L6) | **0 gap** (breath ≤0.25s theo thiết kế) |
V3 + expand (L7) | lead còn 0.7–2.8s ở 8/10 job; 2 job run siêu dài (371s) còn 5.2–5.8s — cần vòng expand thứ 2 hoặc soft lead cap 1.5s |

Phát hiện kèm theo: tỉ lệ voice/source 0.84 nghĩa là **~mọi run đều hụt** (5–45 run/job bị flag).
Muốn giảm số lần expand: **L4 translate không budget phải cho text dài hơn** (baseline bị budget ép
ngắn), khi đó expand chỉ còn là lưới an toàn.

## L4.5 — EDIT: vietsub "có hồn" (1 pass/scene, có gate)

Mô phỏng đúng workflow nhóm vietsub thật: **dịch viên → biên tập viên → kỹ thuật → QC**.
Chỗ "hồn" nằm ở biên tập, không nằm ở dịch.

**Vai trò**: translate = draft trung thành, đủ nghĩa (chưa cần hay). EDIT = viết lại *cách kể*
theo scene, sau đó **chữ đóng băng** — reflow/timeline không được đụng vào câu từ.

**Rubric "hồn" (5 tiêu chí cho editor)**:

1. **Thoát ý, không thoát nghĩa** — kể lại như người Việt, bỏ cấu trúc/âm Hán-Việt sống sượng.
2. **Nhịp kể** — câu ngắn xen câu dài, dừng đúng chỗ cao trào, không đều đều như văn đọc.
3. **Giọng nhân vật** — xưng hô + cách nói theo vai (già/nhõng, lạnh/lì, hài), nhất quán cả phim.
4. **Việt hoá khẩu ngữ** — idiom/điển cố → nghĩa thường ("先到先得" → "ai nhanh chân thì được", không "tiên đáo tiên đắc").
5. **Cảm xúc ở cao trào** — giữ/nhấn nút thắt, câu chốt phải đắt.

**Được phép**: đổi từ chung → động từ mạnh; gộp câu vụn; đảo trật tự trong câu; thêm nhịp/từ nối khẩu ngữ; tăng kịch tính cùng sự thật.
**Cấm**: thêm/bớt sự kiện; đổi ai-làm-gì; thêm số/tên mới; spoil; bình luận ngoài thoại.

**Gate sau edit** (patch vi phạm → giữ draft cho đúng câu đó):

- fact tokens bảo toàn 100% (số liệu, tên glossary, phủ định, chủ thể hành động)
- ⟂ giữ nguyên; không Hán tự; độ dài ±25% so với draft
- tối đa **1 pass + 1 fix pass/scene**; sau đó chữ đóng băng

**Acceptance hai trục**: fidelity = 100% fact tokens · engagement = % scene giữ bản edited (>80%)
+ spot-check người 5-10%. Sau edit, timing chỉ được xử bằng L6 (tempo/breath/lead);
micro-repair ≤1 lần/câu chỉ khi bất khả thi về thời gian.

## Thứ tự implement

1. `units.py` (units + runs) + tests — deterministic.
2. `timeline.py` (planner L6) + tests + simulation trên 10 job để chứng minh trước khi wire.
3. `translate.py` viết lại (unit-level, bỏ budget) + prompts.
4. `reflow.py` + `fit.py` (run-level) + `qa.py` gates.
5. `normalize.py` (L1) + glossary v2.
6. Wire `AUTOREUP_PIPELINE_V3`, chạy 2 job thật, đạt target → xoá legacy.

## Xoá khi cut-over xong

`_format_anchor_rows`/`_budget_chars`, caption-split sớm trong translate, `MEMORY`,
`plan_placements` cũ, polish pass (đã park ở `wip/translate-polish-experiments`),
threshold underfill theo anchor.
