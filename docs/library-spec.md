# AutoReup — Library / Series spec (ĐÃ CHỐT 2026-07-01)

> Quyết định đã khóa: **A. SQLite** (`data/library.db`) · **B. Xóa series = cascade** xóa hết tập+dub+file+export ·
> **C. Ghép = nối thẳng**, không card chuyển tập · **D. 1 tập → 1 series** (episode unique theo platform_video_id).


Mục tiêu: quản lý **hàng ngàn series**, mỗi series gắn **nhiều link kênh đa nền tảng**, chứa **tập** →
chọn tập **dịch** → quản lý **bản đã dịch** → chọn + sắp thứ tự → **ghép export** 1 video dài cho YouTube.

## Phân cấp

```
SERIES (ngàn)  ── sources[] (Douyin/Bilibili/YouTube …)
   ├─ EPISODE (tập nguồn, quét từ sources)      dub_state: none/translating/dubbed/failed
   ├─ (DUBBED = job xong, trỏ về episode+series)
   └─ EXPORT (chọn N tập đã dịch, sắp thứ tự → 1 mp4 dài + tiêu đề/mô tả YT)
```

## Lưu trữ: **SQLite** (`data/library.db`)  ⟵ quyết định chính

JSON `state.json` hiện tại load/save cả file → KHÔNG hợp ngàn series + chục ngàn tập.
Đề xuất SQLite: truy vấn/lọc/phân trang nhanh, ràng buộc unique để dedup tập, đếm theo series rẻ.
Jobs vẫn là file manifest như cũ (chỉ thêm 2 cột liên kết). Bảng:

```sql
series(
  id TEXT PK, title TEXT, cover_url TEXT, tags TEXT(json),
  created_at TEXT, updated_at TEXT)

series_source(
  id TEXT PK, series_id TEXT FK, platform TEXT, url TEXT,
  label TEXT, last_scanned_at TEXT)

episode(
  id TEXT PK, series_id TEXT FK, platform TEXT,
  platform_video_id TEXT, source_url TEXT, title TEXT,
  thumbnail_url TEXT, duration REAL, uploader TEXT,
  discovered_at TEXT, dub_job_id TEXT, dub_state TEXT,
  UNIQUE(series_id, platform, platform_video_id))   -- dedup tập

export(
  id TEXT PK, series_id TEXT FK, title TEXT, description TEXT,
  hashtags TEXT(json), episode_job_ids TEXT(json ordered),
  output_path TEXT, duration REAL, state TEXT, error TEXT,
  created_at TEXT, updated_at TEXT)
```

`dub_state ∈ {none, translating, dubbed, failed}` (suy từ job state, cache vào episode).
`export.state ∈ {draft, rendering, done, failed}`.

## Thay đổi thực thể có sẵn

- **JobManifest**: thêm `series_id: str|None`, `source_video_id: str|None` (trỏ episode). Khi tạo job
  từ một tập → set 2 field này; job xong → cập nhật `episode.dub_state=dubbed`, `dub_job_id`.
- **SourceVideo** (studio): được thay/đồng bộ bởi bảng `episode` (cùng field). Discovery upsert vào `episode`.

## API

```
# Series / Library
GET    /series?q=&sort=&limit=&offset=     liệt kê (search + phân trang)  -> {items, total}
POST   /series                              {title, sources?[]}            -> Series
GET    /series/{id}                         detail + counts (episodes, dubbed, exports)
PATCH  /series/{id}                         {title?, tags?, cover_url?}
DELETE /series/{id}?purge=true|false        xóa series (purge: xóa cả tập/dub/file)
POST   /series/{id}/sources                 {platform, url}                -> thêm link kênh
DELETE /series/{id}/sources/{sid}
POST   /series/{id}/scan                    quét mọi source -> upsert episodes (dedup) -> {added, total}

# Episodes
GET    /series/{id}/episodes?dub_state=     liệt kê tập
POST   /series/{id}/translate               {episode_ids[]} -> tạo job hàng loạt, link -> {jobs}

# Exports (ghép)
GET    /series/{id}/exports
POST   /series/{id}/exports                 {title, description, hashtags[], episode_job_ids[](ordered)} -> draft
POST   /exports/{eid}/render                concat (nền) -> output_path
GET    /exports/{eid}                        trạng thái
DELETE /exports/{eid}
GET    /media/<output_path>                  tải video dài (qua mount /media sẵn có)
```

## Luồng

1. **+ Series** → thêm link kênh → **Scan** → episodes upsert (dedup `platform_video_id`).
2. Chọn tập → **Dịch** → tạo jobs (gắn series_id + source_video_id) → episode.dub_state=translating.
3. Job xong → episode.dub_state=dubbed.
4. Tab **Đã dịch** → chọn + kéo sắp thứ tự → nhập tiêu đề/mô tả YT → **Export** → render.

## Ghép video (render export)  ⟵ quyết định kỹ thuật

Các tập có thể khác độ phân giải/codec → **chuẩn hóa rồi nối** (an toàn hơn concat demuxer):
- re-encode mỗi tập về spec chung (1080p, 30fps, h264 + aac) rồi nối, HOẶC concat demuxer nếu đồng nhất.
- (tùy chọn) chèn **card chuyển tập** ngắn (0.5s logo/tựa) giữa các tập.
- Output 1 file `data/exports/<series>/<export_id>.mp4`.

## Quyết định đã chốt
- **A. SQLite** `data/library.db` cho library; jobs vẫn là file manifest (thêm series_id + source_video_id).
- **B. Cascade**: `DELETE /series/{id}` xóa luôn episodes + job dub + file (video/sub/voice/package) + exports. Bỏ tham số purge (luôn cascade).
- **C. Nối thẳng**: render export concat liền mạch, không card chuyển tập.
- **D. 1 tập → 1 series**: `episode.UNIQUE(series_id, platform, platform_video_id)`; mỗi video gốc thuộc đúng 1 series.

## Thứ tự build (sau khi khóa model)
- **P1**: store SQLite + entity Series/Episode/Export + API; bước **Ghép & Export** (chọn N dub → nối → 1 mp4 + tiêu đề/mô tả YT).
- **P2**: UI Thư viện Series + Series detail (3 tab), gắn discovery kênh vào series.
- **P3**: đa nền tảng (Bilibili/YouTube), search/lọc/lazy-load ngàn series, dedup nâng cao.
