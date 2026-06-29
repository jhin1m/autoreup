import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  Edge,
  Handle,
  Node,
  NodeProps,
  Position,
  ReactFlow,
} from "@xyflow/react";

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

const STAGES = [
  "download",
  "asr",
  "glossary",
  "translate",
  "edit",
  "qa",
  "voiceover",
  "package",
];

const STAGE_LABELS: Record<string, { title: string; detail: string }> = {
  download: { title: "Tải video", detail: "Lấy nguồn gốc" },
  asr: { title: "Tách thoại", detail: "ASR + dựng rail anchor" },
  glossary: { title: "Glossary", detail: "Trích thuật ngữ theo video" },
  translate: { title: "Dịch thoại", detail: "Lời Việt theo chunk" },
  edit: { title: "Biên tập", detail: "Rà soát ngữ nghĩa" },
  qa: { title: "Kiểm tra", detail: "Soát coverage & timing" },
  voiceover: { title: "Lồng tiếng", detail: "TTS + sửa timing" },
  package: { title: "Đóng gói", detail: "Xuất file tải về" },
};

const DOWNLOADS = [
  ["video", "Video gốc"],
  ["subtitle_srt", "SRT"],
  ["subtitle_vtt", "VTT"],
  ["subtitle_json", "JSON"],
  ["soft_sub_mp4", "MP4 phụ đề"],
  ["voice_track", "Voice M4A"],
  ["dubbed_mp4", "MP4 voice"],
  ["tts_manifest", "TTS log"],
  ["zip_package", "ZIP"],
  ["qa_report", "QA"],
  ["pipeline_audit", "Prompt audit"],
];

function FlowNode({ data }: NodeProps<Node<{ label: string; detail: string; status: StageStatus }>>) {
  return (
    <div className={`flow-node ${data.status}`}>
      <Handle type="target" position={Position.Left} />
      <div className="node-status" />
      <div>
        <strong>{data.label}</strong>
        <span>{data.detail}</span>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { flowNode: FlowNode };

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
    if (!response.ok) throw new Error("Không tải được danh sách job");
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
  const stageMap = new Map(activeJob?.stages.map((stage) => [stage.name, stage]));
  const progress = activeJob ? getProgress(activeJob, stages.length) : 0;
  const videoArtifact = activeJob ? getLatestArtifact(activeJob, "video") : undefined;
  const vttArtifact = activeJob ? getLatestArtifact(activeJob, "subtitle_vtt") : undefined;
  const currentCue = useMemo(() => findCue(subtitles, currentTime), [subtitles, currentTime]);

  const nodes = useMemo<Node[]>(
    () =>
      stages.map((stageName, index) => {
        const label = STAGE_LABELS[stageName] ?? { title: stageName, detail: "Đang xử lý" };
        return {
          id: stageName,
          type: "flowNode",
          position: { x: index * 190, y: index % 2 === 0 ? 36 : 118 },
          data: { label: label.title, detail: label.detail, status: stageMap.get(stageName)?.status ?? "pending" },
        };
      }),
    [activeJob?.updated_at, stages.join("|")],
  );

  const edges = useMemo<Edge[]>(
    () =>
      stages.slice(0, -1).map((stageName, index) => ({
        id: `${stageName}-${stages[index + 1]}`,
        source: stageName,
        target: stages[index + 1],
        animated: activeJob?.state === "running",
        className: "flow-edge",
      })),
    [activeJob?.state, stages.join("|")],
  );

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
      setMessage(error instanceof Error ? error.message : "Không tạo được job");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="app-shell">
      <section className="topbar">
        <div className="brand-block">
          <p className="eyebrow">AutoReup Studio</p>
          <h1>Video → phụ đề Việt → file xuất bản</h1>
        </div>
        <form className="submit-card" onSubmit={submitJob}>
          <label htmlFor="video-url">Video link</label>
          <div className="url-row">
            <input
              id="video-url"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://..."
              required
              minLength={8}
            />
            <button disabled={isSubmitting || !url.trim()}>{isSubmitting ? "Đang tạo" : "Chạy"}</button>
          </div>
          {message ? <p className="error-text">{message}</p> : null}
        </form>
      </section>

      <section className="job-strip panel">
        <div className="panel-heading compact-heading">
          <span>Job gần đây</span>
          <button className="ghost-button" onClick={() => loadJobs().catch((error) => setMessage(error.message))}>Làm mới</button>
        </div>
        <div className="job-list horizontal">
          {jobs.length ? (
            jobs.map((job) => (
              <button
                key={job.job_id}
                className={`job-item ${activeJob?.job_id === job.job_id ? "active" : ""}`}
                onClick={() => setActiveJob(job)}
              >
                <span>{job.job_id}</span>
                <small>{job.state}</small>
              </button>
            ))
          ) : (
            <p className="empty-text">Chưa có job.</p>
          )}
        </div>
      </section>

      <section className="studio-grid">
        <section className="preview-panel panel">
          <div className="panel-heading">
            <div>
              <span>{activeJob ? `Preview job ${activeJob.job_id}` : "Preview"}</span>
              <small>{activeJob?.input.url ?? "Dán link để bắt đầu"}</small>
            </div>
            <StatePill state={activeJob?.state ?? "queued"} />
          </div>
          <div className="player-card">
            {videoArtifact ? (
              <div className="video-shell">
                <video
                  ref={videoRef}
                  controls
                  playsInline
                  src={mediaUrl(videoArtifact)}
                  onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
                >
                  {vttArtifact ? <track kind="subtitles" src={`/api/v1/jobs/${activeJob?.job_id}/artifacts/subtitle_vtt`} srcLang="vi" label="Tiếng Việt" /> : null}
                </video>
                <div className={`subtitle-overlay ${currentCue ? "visible" : ""}`}>{currentCue?.translated || currentCue?.text || ""}</div>
              </div>
            ) : (
              <div className="empty-player">
                <strong>Chưa có video</strong>
                <span>Video player sẽ hiện ngay sau bước tải video.</span>
              </div>
            )}
          </div>

          <div className="flow-card">
            <div className="flow-title">
              <span>Realtime pipeline</span>
              <b>{progress}%</b>
            </div>
            <div className="progress-track"><div style={{ width: `${progress}%` }} /></div>
            <div className="canvas-wrap">
              <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.45} maxZoom={1.4} proOptions={{ hideAttribution: true }}>
                <Background gap={20} size={1} />
                <Controls showInteractive={false} />
              </ReactFlow>
            </div>
          </div>
        </section>

        <aside className="side-panel panel">
          <div className="panel-heading"><span>Phụ đề & file</span></div>
          {activeJob ? (
            <>
              {activeJob.error ? <p className="error-box">{activeJob.error}</p> : null}
              <div className="download-grid">
                {DOWNLOADS.map(([kind, label]) => {
                  const available = getLatestArtifact(activeJob, kind);
                  return available ? (
                    <a key={kind} href={`/api/v1/jobs/${activeJob.job_id}/artifacts/${kind}`}>{label}</a>
                  ) : (
                    <span key={kind}>{label}</span>
                  );
                })}
              </div>
              <div className="now-card">
                <span>Đang hiển thị</span>
                <p>{currentCue?.translated || currentCue?.text || "Play video để xem subtitle chạy realtime."}</p>
              </div>
              <h2>Danh sách phụ đề</h2>
              <div className="subtitle-list">
                {subtitles.length ? subtitles.slice(0, 24).map((cue, index) => (
                  <button
                    key={`${cue.start}-${index}`}
                    className={`subtitle-cue ${currentCue === cue ? "active" : ""}`}
                    onClick={() => seekVideo(videoRef.current, cue.start)}
                  >
                    <time>{formatTime(cue.start)} → {formatTime(cue.end)}</time>
                    <p>{cue.translated || cue.text || "—"}</p>
                    {cue.source ? <small>{cue.source}</small> : null}
                  </button>
                )) : <p className="empty-text">Preview sẽ hiện khi có phụ đề JSON.</p>}
              </div>
            </>
          ) : (
            <p className="empty-text">Chọn hoặc tạo job để xem file tải về.</p>
          )}
        </aside>
      </section>
    </main>
  );
}

function StatePill({ state }: { state: JobState }) {
  return <span className={`state-pill ${state}`}>{state}</span>;
}

async function fetchJob(jobId: string) {
  const response = await fetch(`/api/v1/jobs/${jobId}`);
  if (!response.ok) throw new Error("Không tải được job");
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
  const score = job.stages.reduce((total, stage) => total + (stage.status === "succeeded" ? 1 : stage.status === "running" ? 0.5 : 0), 0);
  return Math.max(4, Math.min(100, Math.round((score / Math.max(totalStages, 1)) * 100)));
}

function formatTime(value?: number) {
  if (typeof value !== "number") return "--:--";
  const minutes = Math.floor(value / 60).toString().padStart(2, "0");
  const seconds = Math.floor(value % 60).toString().padStart(2, "0");
  return `${minutes}:${seconds}`;
}
