import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

type JobState = "queued" | "running" | "succeeded" | "failed" | "canceled";
type StageStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";

type Artifact = {
  kind: string;
  path: string;
  mime_type: string;
  size_bytes: number;
  created_at: string;
};

type Stage = {
  name: string;
  status: StageStatus;
  started_at: string | null;
  ended_at: string | null;
  artifacts: Artifact[];
  metrics: Record<string, unknown>;
  logs: string[];
  error: string | null;
};

type JobManifest = {
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

type JobResponse = { job: JobManifest };
type SubtitleCue = { start?: number; end?: number; translated?: string; text?: string; source?: string };

const STAGES = ["download", "asr", "glossary", "translate", "edit", "qa", "voiceover", "package"];

const STAGE_LABELS: Record<string, { title: string; detail: string }> = {
  download: { title: "Nguồn", detail: "Tải video gốc" },
  asr: { title: "Tách thoại", detail: "ASR + rail anchor" },
  glossary: { title: "Thuật ngữ", detail: "Trích theo video" },
  translate: { title: "Dịch", detail: "Lời Việt theo chunk" },
  edit: { title: "Biên tập", detail: "Rà soát ngữ nghĩa" },
  qa: { title: "Kiểm tra", detail: "Coverage & timing" },
  voiceover: { title: "Lồng tiếng", detail: "TTS + sửa timing" },
  package: { title: "Đóng gói", detail: "Xuất file" },
};

const OUTPUTS: [string, string][] = [
  ["dubbed_mp4", "MP4 lồng tiếng"],
  ["soft_sub_mp4", "MP4 phụ đề"],
  ["voice_track", "Giọng đọc"],
  ["subtitle_srt", "SRT"],
  ["subtitle_vtt", "VTT"],
  ["subtitle_json", "JSON"],
  ["zip_package", "Gói ZIP"],
  ["qa_report", "Báo cáo QA"],
];

export function App() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [url, setUrl] = useState("");
  const [jobs, setJobs] = useState<JobManifest[]>([]);
  const [activeJob, setActiveJob] = useState<JobManifest | null>(null);
  const [subtitles, setSubtitles] = useState<SubtitleCue[]>([]);
  const [currentTime, setCurrentTime] = useState(0);
  const [message, setMessage] = useState("");
  const [isSubmitting, setSubmitting] = useState(false);

  const loadJobs = useCallback(async () => {
    const response = await fetch("/api/v1/jobs");
    if (!response.ok) throw new Error("Không tải được danh sách công việc");
    const payload = (await response.json()) as JobResponse[];
    const recent = payload.map((item) => item.job);
    setJobs(recent);
    setActiveJob((current) => current ?? recent[0] ?? null);
  }, []);

  useEffect(() => {
    loadJobs().catch((error) => setMessage(error.message));
  }, [loadJobs]);

  useEffect(() => {
    if (!activeJob) return;
    setSubtitles([]);
    if (["succeeded", "failed", "canceled"].includes(activeJob.state)) {
      loadSubtitlePreview(activeJob.job_id).then(setSubtitles).catch(() => setSubtitles([]));
      return;
    }
    const events = new EventSource(`/api/v1/jobs/${activeJob.job_id}/events`);
    events.onmessage = (event) => {
      const job = JSON.parse(event.data) as JobManifest;
      setActiveJob(job);
      setJobs((items) => upsertJob(items, job));
      if (["succeeded", "failed", "canceled"].includes(job.state)) {
        events.close();
        loadSubtitlePreview(job.job_id).then(setSubtitles).catch(() => setSubtitles([]));
      }
    };
    events.onerror = () => {
      events.close();
      fetchJob(activeJob.job_id).then(setActiveJob).catch(() => undefined);
    };
    return () => events.close();
  }, [activeJob?.job_id]);

  const stages = activeJob?.config?.stages?.length ? activeJob.config.stages : STAGES;
  const stageMap = useMemo(() => new Map(activeJob?.stages.map((stage) => [stage.name, stage])), [activeJob]);
  const progress = activeJob ? getProgress(activeJob, stages.length) : 0;
  const activeStageName = stages.find((name) => stageMap.get(name)?.status === "running");
  const videoArtifact = activeJob ? getLatestArtifact(activeJob, "video") : undefined;
  const vttArtifact = activeJob ? getLatestArtifact(activeJob, "subtitle_vtt") : undefined;
  const currentCue = useMemo(() => findCue(subtitles, currentTime), [subtitles, currentTime]);

  async function submitJob(event: FormEvent) {
    event.preventDefault();
    setMessage("");
    setSubmitting(true);
    try {
      const response = await fetch("/api/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url.trim() }),
      });
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as JobResponse;
      setUrl("");
      setActiveJob(payload.job);
      setJobs((items) => upsertJob(items, payload.job));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không tạo được công việc");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="desk">
      <header className="deck">
        <div className="mark">
          <span className="mark-dot" aria-hidden />
          <div>
            <p className="mark-name">AutoReup</p>
            <p className="mark-sub">Localization Desk · 中文 → Tiếng Việt</p>
          </div>
        </div>
        <form className="source" onSubmit={submitJob}>
          <span className="source-tag">NGUỒN</span>
          <input
            aria-label="Đường dẫn video nguồn"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="Dán link Douyin / Bilibili…"
            required
            minLength={8}
          />
          <button disabled={isSubmitting || !url.trim()}>{isSubmitting ? "Đang dựng…" : "Dựng bản dub"}</button>
        </form>
      </header>

      {message ? <p className="banner">{message}</p> : null}

      <Transport stages={stages} stageMap={stageMap} state={activeJob?.state} progress={progress} activeStage={activeStageName} />

      <main className="stage-grid">
        <section className="screen panel">
          <div className="panel-head">
            <div className="panel-title">
              <h2>Xem trước</h2>
              <p className="mono small">{activeJob ? activeJob.input.url : "Chưa chọn công việc"}</p>
            </div>
            <StatePill state={activeJob?.state ?? "queued"} />
          </div>
          <div className="viewport">
            {videoArtifact ? (
              <>
                <video
                  ref={videoRef}
                  controls
                  playsInline
                  src={mediaUrl(videoArtifact)}
                  onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
                >
                  {vttArtifact ? (
                    <track kind="subtitles" src={`/api/v1/jobs/${activeJob?.job_id}/artifacts/subtitle_vtt`} srcLang="vi" label="Tiếng Việt" default />
                  ) : null}
                </video>
                <div className={`overlay ${currentCue ? "lit" : ""}`}>{cueText(currentCue)}</div>
              </>
            ) : (
              <div className="viewport-empty">
                <span className="reel" aria-hidden />
                <strong>Màn hình chờ nguồn</strong>
                <p>Video hiện ngay sau bước tải. Phụ đề tiếng Việt chạy realtime cùng tiến trình.</p>
              </div>
            )}
          </div>
          <div className="now">
            <span className="now-tag">ĐANG ĐỌC</span>
            <p>{cueText(currentCue) || "Phát video để theo dõi lời thoại."}</p>
          </div>
        </section>

        <aside className="sheet panel">
          <div className="panel-head">
            <div className="panel-title">
              <h2>Cue sheet</h2>
              <p className="mono small">{subtitles.length ? `${subtitles.length} dòng tiếng Việt` : "Chưa có phụ đề"}</p>
            </div>
          </div>
          {activeJob?.error ? <p className="fault">{activeJob.error}</p> : null}
          <div className="cues">
            {subtitles.length ? (
              subtitles.map((cue, index) => (
                <button
                  key={`${cue.start}-${index}`}
                  className={`cue ${currentCue === cue ? "on" : ""}`}
                  onClick={() => seekVideo(videoRef.current, cue.start)}
                >
                  <time className="mono">{formatTime(cue.start)}</time>
                  <p>{cue.translated || cue.text || "—"}</p>
                </button>
              ))
            ) : (
              <p className="muted-note">Cue sheet hiện khi bước Kiểm tra hoàn tất.</p>
            )}
          </div>
          <div className="outputs">
            <span className="outputs-tag">XUẤT FILE</span>
            <div className="chips">
              {OUTPUTS.map(([kind, label]) => {
                const available = activeJob ? getLatestArtifact(activeJob, kind) : undefined;
                return available ? (
                  <a key={kind} className="chip ready" href={`/api/v1/jobs/${activeJob!.job_id}/artifacts/${kind}`}>
                    {label}
                  </a>
                ) : (
                  <span key={kind} className="chip">
                    {label}
                  </span>
                );
              })}
            </div>
          </div>
        </aside>
      </main>

      <footer className="ledger panel">
        <div className="ledger-head">
          <span className="ledger-tag">CÔNG VIỆC GẦN ĐÂY</span>
          <button className="link" onClick={() => loadJobs().catch((error) => setMessage(error.message))}>
            Làm mới
          </button>
        </div>
        <div className="ledger-row">
          {jobs.length ? (
            jobs.map((job) => (
              <button
                key={job.job_id}
                className={`ticket ${activeJob?.job_id === job.job_id ? "active" : ""}`}
                onClick={() => setActiveJob(job)}
              >
                <span className="mono ticket-id">{job.job_id}</span>
                <span className={`ticket-state ${job.state}`}>{STATE_VI[job.state]}</span>
              </button>
            ))
          ) : (
            <p className="muted-note">Chưa có công việc nào. Dán một link để bắt đầu.</p>
          )}
        </div>
      </footer>
    </div>
  );
}

function Transport({
  stages,
  stageMap,
  state,
  progress,
  activeStage,
}: {
  stages: string[];
  stageMap: Map<string, Stage>;
  state?: JobState;
  progress: number;
  activeStage?: string;
}) {
  const running = state === "running";
  return (
    <section className={`transport ${running ? "live" : ""}`}>
      <div className="transport-head">
        <span className="transport-tag">TIẾN TRÌNH</span>
        <span className="transport-now mono">
          {activeStage ? STAGE_LABELS[activeStage]?.title ?? activeStage : state === "succeeded" ? "Hoàn tất" : "Chờ chạy"}
        </span>
        <span className="transport-pct mono">{progress}%</span>
      </div>
      <div className="rail">
        <div className="rail-line" />
        <div className="rail-fill" style={{ width: `${progress}%` }} />
        <ol className="stops">
          {stages.map((name, index) => {
            const status = stageMap.get(name)?.status ?? "pending";
            const label = STAGE_LABELS[name] ?? { title: name, detail: "" };
            return (
              <li key={name} className={`stop ${status}`}>
                <span className="stop-node" aria-hidden />
                <span className="stop-index mono">{String(index + 1).padStart(2, "0")}</span>
                <span className="stop-title">{label.title}</span>
                <span className="stop-detail">{label.detail}</span>
              </li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}

function StatePill({ state }: { state: JobState }) {
  return (
    <span className={`pill ${state}`}>
      <span className="pill-dot" aria-hidden />
      {STATE_VI[state]}
    </span>
  );
}

const STATE_VI: Record<JobState, string> = {
  queued: "Chờ",
  running: "Đang chạy",
  succeeded: "Hoàn tất",
  failed: "Lỗi",
  canceled: "Đã huỷ",
};

async function fetchJob(jobId: string) {
  const response = await fetch(`/api/v1/jobs/${jobId}`);
  if (!response.ok) throw new Error("Không tải được công việc");
  const payload = (await response.json()) as JobResponse;
  return payload.job;
}

async function loadSubtitlePreview(jobId: string) {
  const response = await fetch(`/api/v1/jobs/${jobId}/artifacts/subtitle_json`);
  if (!response.ok) return [];
  const payload = await response.json();
  return Array.isArray(payload) ? (payload as SubtitleCue[]) : [];
}

function upsertJob(items: JobManifest[], job: JobManifest) {
  return [job, ...items.filter((item) => item.job_id !== job.job_id)];
}

function getLatestArtifact(job: JobManifest, kind: string) {
  return [...job.artifacts].reverse().find((artifact) => artifact.kind === kind);
}

function mediaUrl(artifact: Artifact) {
  return `/media/${artifact.path}`;
}

function cueText(cue?: SubtitleCue) {
  return cue?.translated || cue?.text || "";
}

function findCue(cues: SubtitleCue[], time: number) {
  return cues.find((cue) => typeof cue.start === "number" && typeof cue.end === "number" && time >= cue.start && time <= cue.end);
}

function seekVideo(video: HTMLVideoElement | null, time?: number) {
  if (!video || typeof time !== "number") return;
  video.currentTime = Math.max(time + 0.01, 0);
  video.play().catch(() => undefined);
}

function getProgress(job: JobManifest, totalStages: number) {
  if (job.state === "succeeded") return 100;
  const score = job.stages.reduce(
    (total, stage) => total + (stage.status === "succeeded" ? 1 : stage.status === "running" ? 0.5 : 0),
    0,
  );
  return Math.max(2, Math.min(100, Math.round((score / Math.max(totalStages, 1)) * 100)));
}

function formatTime(value?: number) {
  if (typeof value !== "number") return "––:––";
  const minutes = Math.floor(value / 60).toString().padStart(2, "0");
  const seconds = Math.floor(value % 60).toString().padStart(2, "0");
  return `${minutes}:${seconds}`;
}
