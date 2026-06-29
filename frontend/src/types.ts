export type JobState = "queued" | "running" | "succeeded" | "failed" | "canceled";
export type StageStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";

export type Artifact = {
  kind: string;
  path: string;
  mime_type: string;
  size_bytes: number;
  created_at: string;
};

export type Stage = {
  name: string;
  status: StageStatus;
  started_at: string | null;
  ended_at: string | null;
  artifacts: Artifact[];
  metrics: Record<string, unknown>;
  logs: string[];
  error: string | null;
};

export type JobManifest = {
  job_id: string;
  input: { url: string };
  state: JobState;
  config?: { stages?: string[] };
  stages: Stage[];
  artifacts: Artifact[];
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type JobResponse = { job: JobManifest };

export type SubtitleCue = {
  start?: number;
  end?: number;
  translated?: string;
  text?: string;
  source?: string;
  anchor_id?: number;
};

export type GlossaryEntry = { term_zh: string; term_vi: string; kind: string; note: string };

export type ProviderSession = {
  platform: string;
  label: string;
  configured: boolean;
  updated_at: string | null;
  cookie_count: number;
  cookie_names: string[];
  required_missing: string[];
  recommended_missing: string[];
  note: string;
};

export type SourceVideo = {
  id: string;
  platform: string;
  platform_video_id: string;
  source_url: string;
  title: string;
  thumbnail_url: string | null;
  duration: number | null;
  uploader: string | null;
  download_state: string;
};
