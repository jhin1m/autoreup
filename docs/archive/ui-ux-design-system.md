# AutoReup Studio UI/UX Design System

## Product frame

AutoReup UI is a video management studio, not a pipeline/debug console. The user-facing model is:

```text
Video library -> import URL -> processing status -> preview subtitles -> download outputs
```

Server/runtime settings such as ASR worker, LLM provider, model, base URL, cookies, and test fixtures are never shown in the end-user UI.

## 2026 stack

- Svelte 5 + Vite for a fast interactive dashboard.
- Tailwind CSS 4 with CSS-first design tokens in `src/app.css`.
- Component-first layout under `frontend/src/components`.
- FastAPI serves the production build from `frontend/dist`.

## Reference patterns researched

- YouTube Studio: left content library, top search/import, video detail page, status and subtitle management.
- Modern SaaS dashboards: F-pattern hierarchy, status colors only for meaningful state, operational control panel for live jobs.
- Svelte/Tailwind 2026 conventions: Vite plugin order, Tailwind v4 CSS tokens, reusable components instead of one large page file.

## UX laws and rules to obey

- **Nielsen heuristic — visibility of system status**: every job must show current state, current stage, progress, and final result.
- **Nielsen heuristic — match real world**: user sees `video`, `phụ đề`, `tải xuống`, not provider/model/process jargon.
- **Nielsen heuristic — consistency**: the same state uses the same label, color, placement, and component across library/detail/timeline.
- **Nielsen heuristic — error recovery**: errors are plain Vietnamese with next action; raw stack/provider errors stay out of user copy.
- **Hick's Law**: one primary action per surface. Import URL is the only top-level CTA.
- **Fitts's Law / WCAG target size**: frequent controls must be easy to hit; interactive controls should be at least 40px where practical, never below WCAG 2.2 minimum target size.
- **Jakob's Law**: content management should follow familiar studio/dashboard patterns: sidebar library, search/filter, detail page, exports.
- **Gestalt proximity and hierarchy**: related controls live together; top-left content answers “what is this video and what is its status?” immediately.
- **Aesthetic minimalist design**: no decorative AI gradients, fake nav, or unused controls; every visible action must work.
- **WCAG 2.2**: visible focus, keyboard-reachable controls, text labels for inputs, contrast-safe status indicators, no color-only meaning.

## Navigation model

- Top bar: product identity and single primary import action.
- Left sidebar: video library, high-level filters, search by URL/job ID.
- Main detail: selected video overview, status, preview player, processing timeline, QA metrics, subtitle list, downloads.
- Empty states are first-class and explain the next user action.

## Visual language

- Dark studio theme for video/subtitle work.
- Neutral surfaces: `canvas`, `shell`, `panel`, `panel-soft`, `panel-raised`.
- One primary accent: blue for active/import/progress.
- Semantic colors only for state: green success, amber canceled/warning, red failed.
- Rounded 2xl cards, consistent borders, restrained shadows.
- No gradient-heavy or “AI generated” decorative style.

## Vietnamese copy rules

- Use Vietnamese labels consistently: `Xem trước`, `Tiến trình xử lý`, `Phụ đề`, `Tải xuống`.
- Avoid implementation jargon in UI: no `LLM`, `ASR worker`, `provider`, `base URL`, `debug`, `golden`, `rerender`.
- Technical metrics can appear only as product quality signals, e.g. `Độ dài TB`, `Tốc độ đọc`, `Lỗi QA`.
- Error text should be actionable and user-safe; internal exception text stays in server logs/manifests.

## Subtitle preview policy

Subtitle overlay size is not hardcoded. The player computes font size from the rendered video box:

- width ratio
- height ratio
- min/max box ratio
- user scale preference
- responsive bottom/side safe margins

This keeps subtitles readable across dashboard, mobile, and fullscreen layouts.
