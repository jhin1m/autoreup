# AutoReup Studio UI/UX Refactor Plan

## Current state verified

- The latest Douyin job `e07f29ec62bc` completed end-to-end with QA `issues=0` and generated preview/download artifacts.
- The current dashboard renders the intended studio model: library, import URL, job status, preview, subtitle list, downloads.
- Browser accessibility snapshot confirms the main controls are reachable, but several product-level UX leaks remain.

## Research takeaways

- Studio tools should be title-centric: every screen starts from the video/title, then status, review, QA, and exports hang off that object.
- Dashboards need layered hierarchy: global import/search, contextual job actions, primary review surface, then secondary quality/export details.
- Job status UI should show visibility of system status without exposing implementation logs.
- Empty and error states should say what happened, why if safe, and what the next action is.
- Video review should keep native media controls where possible for accessibility, with clear captions, contrast, keyboard reachability, and visible focus.
- Advanced/internal data should use progressive disclosure; the default export surface should only show user-ready files.

## UX problems found in current UI

1. **Downloads expose intermediate artifacts**
   - The panel shows repeated JSON/SRT files from initial, compressed, reflow, and final stages.
   - This makes the app feel like a pipeline console instead of a video studio.

2. **Timeline can leak raw stage logs**
   - `StageTimeline` displays the last log line. For successful stages this can be raw JSON braces or file paths.
   - Users need readable stage summaries, not script output.

3. **Video title is underused**
   - The detail header and library currently show mostly the URL hostname.
   - The downloaded video title is available in `metrics.download.title` and should become the primary label once known.

4. **Running/pending stage visibility is incomplete**
   - Only started stages are rendered, while total progress comes from the configured stage list.
   - Users should see the whole expected path with pending/running/done states.

5. **Main detail hierarchy is still too panel-heavy**
   - Preview, progress, metrics, subtitle list, and downloads all compete equally.
   - After success, the primary decisions are: review subtitles, download package, inspect quality if needed.

6. **Failed state lacks a recovery surface**
   - Error copy is user-safe, but there is no clear retry/check action in the UI.
   - Until backend retry exists, the UI should still offer “Nhập lại video” or “Tạo job mới từ URL này”.

## Target information architecture

```text
Top bar
  Brand + import URL CTA

Sidebar library
  Search/filter + video rows with title, status, duration, updated time

Main job detail
  Job hero: title, URL, status, current stage, primary action
  Review workspace:
    Left/sticky: preview player + subtitle controls
    Right: progress card while running, downloads card after success, error card on failure
  Detail tabs:
    Phụ đề
    Chất lượng
    Tệp tải
```

## Refactor phases

### Phase 0 — UX cleanup hotfix

Scope: keep layout mostly unchanged, remove product regressions.

- Use video title from `metrics.download.title` in library/detail when available.
- Show only user-ready downloads:
  - final SRT
  - VTT
  - soft-sub MP4
  - ZIP package
  - optional final JSON under a less prominent “Dữ liệu phụ đề” label
- Replace raw stage logs with curated stage messages.
- Render all configured stages, including pending stages.
- Improve success/failure empty states in preview, subtitles, and downloads.

Acceptance:

- Browser snapshot no longer shows repeated intermediate JSON/SRT downloads.
- Timeline never shows raw JSON braces, local file paths, provider names, or script logs.
- The completed Douyin job is recognizable by title, not just hostname.

### Phase 1 — Component structure refactor

Scope: make UI easier to evolve without changing API.

- Add derived job helpers in `frontend/src/lib/jobs.ts`:
  - `displayTitle(job)`
  - `currentStage(job)`
  - `progressFor(job)`
  - `publicDownloads(job)`
  - `stageRows(job)`
- Split main page into focused components:
  - `JobHeader.svelte`
  - `JobWorkspace.svelte`
  - `JobQualityPanel.svelte`
  - `PanelShell.svelte`
- Keep UI copy in Vietnamese and avoid runtime/debug vocabulary.

Acceptance:

- `App.svelte` owns page state only; display derivation moves out.
- Existing tests pass, `npm run check` stays clean.

### Phase 2 — Review workflow improvements

Scope: make subtitle review feel like a studio tool.

- Keep preview as the primary surface and make it visually dominant.
- Add auto-scroll/highlight behavior for active subtitle row.
- Add subtitle search/filter by text and timestamp.
- Show compact quality summary:
  - number of subtitle lines
  - QA issues
  - average reading speed
  - warning if many lines need review, phrased as “Cần xem lại”.
- Keep subtitle overlay responsive via current computed layout; no hardcoded font size.

Acceptance:

- User can play video, see active subtitle, jump to subtitle, and download outputs without scanning internal pipeline data.

### Phase 3 — Responsive and accessibility pass

Scope: polish real usage across laptop/mobile/keyboard.

- Collapse sidebar into a library drawer or top selector on mobile.
- Add `aria-live="polite"` for job status changes.
- Ensure every icon-only or compact action has a visible or accessible label.
- Preserve native video controls for accessibility.
- Verify focus order: import → library → job header → preview → timeline/downloads → subtitles.

Acceptance:

- Browser snapshot shows sensible labels for interactive controls.
- Keyboard-only flow can import/select/review/download.
- Mobile layout does not bury the selected job under the library.

## Do not do yet

- No theme rewrite or decorative gradient redesign.
- No subtitle editing workflow until preview/review is stable.
- No provider/model/server config in the UI.
- No hard-sub render workflow for preview.
- No fake controls or non-working navigation.

## Verification checklist

- `npm run check`
- `npm run build`
- `.venv/bin/python -m unittest discover -s tests`
- Browser snapshot for:
  - empty library
  - running job
  - succeeded job
  - failed job
- Manual checks:
  - final downloads only
  - no raw logs in timeline
  - no forbidden runtime/debug words
  - subtitle overlay remains responsive
