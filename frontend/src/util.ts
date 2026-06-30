import type { DubState, ExportState } from "./types";

/** Route a remote CDN cover through the backend proxy (browsers can't load Douyin
 *  images cross-origin). Local /media paths and empty values pass through unchanged. */
export function thumbSrc(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  if (url.startsWith("/")) return url;
  return `/api/v1/library/thumb?url=${encodeURIComponent(url)}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return "";
  const s = Math.round(seconds);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  const mm = m % 60;
  const ss = s % 60;
  return h > 0 ? `${h}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${m}:${String(ss).padStart(2, "0")}`;
}

export const DUB_STATE_VI: Record<DubState, string> = {
  none: "Chưa dịch",
  translating: "Đang dịch",
  dubbed: "Đã dịch",
  failed: "Lỗi",
};

export const EXPORT_STATE_VI: Record<ExportState, string> = {
  draft: "Nháp",
  rendering: "Đang ghép",
  done: "Xong",
  failed: "Lỗi",
};
