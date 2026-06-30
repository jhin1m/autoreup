import type { GlossaryEntry, JobManifest, JobResponse, ProviderSession, SourceVideo, SubtitleCue } from "./types";

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
