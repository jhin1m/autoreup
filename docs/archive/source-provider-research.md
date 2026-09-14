# Source Provider Research: Douyin, Bilibili, YouTube

## Summary

AutoReup should not depend on frontend calls to third-party platforms. The product API should expose normalized sources and source videos, while backend provider adapters handle platform-specific discovery and metadata.

```text
Frontend
  -> AutoReup API
    -> SourceProvider adapter
      -> official API / public web API / browser network capture / yt-dlp fallback
```

## Findings

### YouTube

- Has official APIs suitable for channel management, uploads, playlists, metadata, scheduling, and analytics workflows.
- Use YouTube as the publishing/management target, not as the first source-discovery problem.
- Constraints are mostly OAuth, quota, upload policy checks, and channel authorization.

### Bilibili

Official/open platform:

- Bilibili Open Platform exists and includes video submission/upload capabilities.
- It is useful for authorized publishing workflows, not necessarily arbitrary public source discovery.

Public web endpoints:

- Video metadata endpoint works for public videos:
  - `https://api.bilibili.com/x/web-interface/view?bvid=...`
  - Verified locally: returned `code=0`, title, duration, thumbnail, owner for `BV13x41117TL`.
- User/space video list endpoint exists:
  - `https://api.bilibili.com/x/space/wbi/arc/search`
  - Requires WBI signing and can trigger risk-control responses.
  - Unsigned local test returned `code=-403`.
  - Minimal signed local test returned `code=-352` risk check failure.
  - `yt-dlp` successfully extracted BVIDs from `https://space.bilibili.com/3985676/video`, so the flow is feasible with a mature extractor/browser-like behavior.
- `yt-dlp` uses WBI signing and handles these endpoints in `yt_dlp/extractor/bilibili.py`.

Practical feasibility:

| Capability | Feasibility | Notes |
| --- | --- | --- |
| Single video metadata | High | Public endpoint works. |
| Single video download | High | `yt-dlp` supports public videos; login may be needed for restricted/premium/geo content. |
| User/channel latest videos | Medium-high | WBI/risk control; use provider adapter with browser headers/session and fallback. |
| Collections/playlists | Medium-high | Several public endpoints exist; exact endpoint depends on list type. |
| Massive continuous scan | Medium | Needs cache, rate limits, backoff, retries, and browser/session fallback. |

Recommended Bilibili v1:

1. Extract BVID list via `yt-dlp --flat-playlist` or signed provider.
2. Hydrate each BVID through `x/web-interface/view` for title, thumbnail, duration, owner.
3. Cache normalized records.
4. Only download selected videos.

### Douyin

Official/open platform:

- Douyin Open Platform has video list/data APIs, but they require OAuth/user authorization and approved scopes.
- The account video list endpoint is for authorized accounts, not arbitrary public channel scraping.
- The official doc for account video list notes limited availability and requires `access-token`, `open_id`, cursor pagination.
- Keyword video search also requires authorization/scope and platform-side configuration.

Public web endpoints:

- Single video detail endpoint exists:
  - `https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=...`
  - Verified locally for `7641517549376654626`: returned title and duration JSON.
- `yt-dlp` Douyin extractor uses this endpoint and raises “Fresh cookies are needed” when detail JSON is unavailable.
- User post list endpoint exists in network traffic:
  - `/aweme/v1/web/aweme/post/`
  - Requires browser-like parameters, cookies, `msToken`, and `a_bogus`/signature behavior.
  - Community reports show pagination can break or return empty responses even with cookies/signature.

Practical feasibility:

| Capability | Feasibility | Notes |
| --- | --- | --- |
| Single video metadata | High-best-effort | Current public detail endpoint works for tested item; may require fresh cookies. |
| Single video download | High-best-effort | `yt-dlp` works for tested Douyin URLs; cookies may be needed. |
| Public user/channel scan | Medium-low | Needs browser session/network capture or maintained signing; fragile under anti-bot. |
| Keyword search discovery | Low-medium | Official route requires auth; web route requires signing/cookies and can change. |
| Massive continuous scan | Low without browser/session ops | Requires session management, rate limits, failure queues, manual fallback. |

Recommended Douyin v1:

1. Support single-video URL metadata/download first.
2. Add source/channel scan as best-effort browser-network provider.
3. Capture network responses from a real browser session instead of hardcoding signing in the UI.
4. Cache results and mark scan failures as actionable exceptions.
5. Do not promise complete channel coverage until session/cookie/risk handling is proven.

## Browser network provider

Use when public endpoint direct calls fail or require dynamic browser signatures.

```text
Open source URL in controlled browser session
  -> capture XHR/fetch JSON
  -> match known video-list/detail payloads
  -> normalize SourceVideo records
  -> scroll/page conservatively
  -> cache results
```

This avoids putting platform internals in the frontend, but it is slower and not guaranteed for high-volume scraping.

## Product implication

The first rewrite should build source discovery as a resilient workflow, not a single URL job page:

```text
Sources
  -> Scan
  -> SourceVideos
  -> Select
  -> DownloadQueue
  -> VideoAssets
  -> Processing/Review/Publish
```

## Reliability levels

```text
Stable core:
  YouTube publishing API
  Local inventory/queue/review/export
  Bilibili single video metadata/download
  Douyin single video metadata/download

Best-effort source discovery:
  Bilibili public space/list scan
  Douyin public profile/post scan

Needs operational safeguards:
  session cookies
  rate limits
  scan backoff
  provider health status
  manual URL fallback
  exception center
```

## Sources checked

- Douyin Open Platform account video list: `https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/search-video/account-video-list/`
- Douyin Open Platform keyword video search: `https://partner.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/search-management/keywords-video-list/keywords-video`
- Bilibili API collect video info docs: `https://github.com/pskdje/bilibili-API-collect/blob/main/docs/video/info.md`
- Bilibili user space API examples: `https://api.bilibili.com/x/space/wbi/arc/search`
- `yt-dlp` Bilibili extractor: `yt_dlp/extractor/bilibili.py`
- `yt-dlp` Douyin extractor in installed version: `yt_dlp/extractor/tiktok.py`
