import type {
  ChannelGroup,
  Episode,
  Export,
  GlossaryEntry,
  JobManifest,
  JobResponse,
  ProviderSession,
  Series,
  SeriesPage,
  SeriesSource,
  SourceVideo,
  SubtitleCue,
} from "./types";

async function ok(response: Response, message: string) {
  if (!response.ok) throw new Error((await response.text()) || message);
  return response;
}

export async function listJobs(): Promise<JobManifest[]> {
  const response = await ok(await fetch("/api/v1/jobs"), "Không tải được danh sách công việc");
  const payload = (await response.json()) as JobResponse[];
  return payload.map((item) => item.job);
}

export async function fetchJob(jobId: string): Promise<JobManifest> {
  const response = await ok(await fetch(`/api/v1/jobs/${jobId}`), "Không tải được công việc");
  return ((await response.json()) as JobResponse).job;
}

export async function cancelJob(jobId: string): Promise<JobManifest> {
  const response = await ok(await fetch(`/api/v1/jobs/${jobId}/cancel`, { method: "POST" }), "Không dừng được công việc");
  return ((await response.json()) as JobResponse).job;
}

export async function deleteJob(jobId: string): Promise<void> {
  await ok(await fetch(`/api/v1/jobs/${jobId}`, { method: "DELETE" }), "Không xóa được công việc");
}

export async function createJob(url: string): Promise<JobManifest> {
  const response = await ok(
    await fetch("/api/v1/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }),
    "Không tạo được công việc",
  );
  return ((await response.json()) as JobResponse).job;
}

export async function fetchArtifactJson<T>(jobId: string, kind: string): Promise<T | null> {
  const response = await fetch(`/api/v1/jobs/${jobId}/artifacts/${kind}`);
  if (!response.ok) return null;
  return (await response.json()) as T;
}

export async function loadCues(jobId: string): Promise<SubtitleCue[]> {
  const payload = await fetchArtifactJson<SubtitleCue[]>(jobId, "subtitle_json");
  return Array.isArray(payload) ? payload : [];
}

export async function loadGlossary(jobId: string): Promise<GlossaryEntry[]> {
  const payload = await fetchArtifactJson<GlossaryEntry[]>(jobId, "glossary");
  return Array.isArray(payload) ? payload : [];
}

export async function loadProviders(): Promise<ProviderSession[]> {
  const response = await fetch("/api/v1/providers/sessions");
  if (!response.ok) return [];
  return ((await response.json()) as { sessions: ProviderSession[] }).sessions;
}

export async function saveCookies(platform: string, cookies: string): Promise<void> {
  await ok(
    await fetch(`/api/v1/providers/${platform}/cookies`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookies }),
    }),
    "Không lưu được cookie",
  );
}

export async function discoverVideos(url: string): Promise<SourceVideo[]> {
  const response = await ok(
    await fetch("/api/v1/studio/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, limit: 18 }),
    }),
    "Không quét được nguồn",
  );
  return ((await response.json()) as { videos: SourceVideo[] }).videos;
}

/* ---------- library ---------- */

const LIB = "/api/v1/library";
const jsonHeaders = { "Content-Type": "application/json" };

export async function listSeries(q = ""): Promise<SeriesPage> {
  const response = await ok(await fetch(`${LIB}/series?q=${encodeURIComponent(q)}`), "Không tải được thư viện");
  return (await response.json()) as SeriesPage;
}

export async function previewChannel(url: string): Promise<ChannelGroup[]> {
  const response = await ok(
    await fetch(`${LIB}/preview-channel`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ url }) }),
    "Không quét được kênh",
  );
  return ((await response.json()) as { groups: ChannelGroup[] }).groups;
}

export async function importChannel(url: string, groups: { title: string; episodes: unknown[] }[]): Promise<string[]> {
  const response = await ok(
    await fetch(`${LIB}/import-channel`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ url, groups }) }),
    "Không tạo được bộ",
  );
  return ((await response.json()) as { series_ids: string[] }).series_ids;
}

export async function createSeries(title: string, sources: { platform: string; url: string }[] = []): Promise<Series> {
  const response = await ok(
    await fetch(`${LIB}/series`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ title, sources }) }),
    "Không tạo được series",
  );
  return (await response.json()) as Series;
}

export async function getSeries(id: string): Promise<Series> {
  const response = await ok(await fetch(`${LIB}/series/${id}`), "Không tải được series");
  return (await response.json()) as Series;
}

export async function deleteSeries(id: string): Promise<void> {
  await ok(await fetch(`${LIB}/series/${id}`, { method: "DELETE" }), "Không xóa được series");
}

export async function addSource(seriesId: string, platform: string, url: string): Promise<SeriesSource> {
  const response = await ok(
    await fetch(`${LIB}/series/${seriesId}/sources`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ platform, url }) }),
    "Không thêm được nguồn",
  );
  return (await response.json()) as SeriesSource;
}

export async function deleteSource(seriesId: string, sourceId: string): Promise<void> {
  await ok(await fetch(`${LIB}/series/${seriesId}/sources/${sourceId}`, { method: "DELETE" }), "Không xóa được nguồn");
}

export async function scanSeries(seriesId: string): Promise<{ added: number; total: number }> {
  const response = await ok(await fetch(`${LIB}/series/${seriesId}/scan`, { method: "POST" }), "Không quét được kênh");
  return (await response.json()) as { added: number; total: number };
}

export async function listEpisodes(seriesId: string): Promise<Episode[]> {
  const response = await ok(await fetch(`${LIB}/series/${seriesId}/episodes`), "Không tải được tập");
  return (await response.json()) as Episode[];
}

export async function translateEpisodes(seriesId: string, episodeIds: string[]): Promise<string[]> {
  const response = await ok(
    await fetch(`${LIB}/series/${seriesId}/translate`, { method: "POST", headers: jsonHeaders, body: JSON.stringify({ episode_ids: episodeIds }) }),
    "Không tạo được job dịch",
  );
  return ((await response.json()) as { jobs: string[] }).jobs;
}

export async function listExports(seriesId: string): Promise<Export[]> {
  const response = await ok(await fetch(`${LIB}/series/${seriesId}/exports`), "Không tải được bản ghép");
  return (await response.json()) as Export[];
}

export async function createExport(
  seriesId: string,
  body: { title: string; description: string; hashtags: string[]; episode_job_ids: string[] },
): Promise<Export> {
  const response = await ok(
    await fetch(`${LIB}/series/${seriesId}/exports`, { method: "POST", headers: jsonHeaders, body: JSON.stringify(body) }),
    "Không tạo được bản ghép",
  );
  return (await response.json()) as Export;
}

export async function renderExport(exportId: string): Promise<Export> {
  const response = await ok(await fetch(`${LIB}/exports/${exportId}/render`, { method: "POST" }), "Không render được");
  return (await response.json()) as Export;
}

export async function getExport(exportId: string): Promise<Export> {
  const response = await ok(await fetch(`${LIB}/exports/${exportId}`), "Không tải được bản ghép");
  return (await response.json()) as Export;
}

export async function deleteExport(exportId: string): Promise<void> {
  await ok(await fetch(`${LIB}/exports/${exportId}`, { method: "DELETE" }), "Không xóa được bản ghép");
}
