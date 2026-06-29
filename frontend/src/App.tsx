import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "./api";
import type { GlossaryEntry, JobManifest, JobState, ProviderSession, SourceVideo, Stage, StageStatus, SubtitleCue } from "./types";

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

const STATE_VI: Record<JobState, string> = {
  queued: "Chờ",
  running: "Đang chạy",
  succeeded: "Hoàn tất",
  failed: "Lỗi",
  canceled: "Đã huỷ",
};

type ReviewTab = "bilingual" | "glossary" | "stage";

export function App() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [url, setUrl] = useState("");
  const [jobs, setJobs] = useState<JobManifest[]>([]);
  const [activeJob, setActiveJob] = useState<JobManifest | null>(null);
  const [cues, setCues] = useState<SubtitleCue[]>([]);
  const [glossary, setGlossary] = useState<GlossaryEntry[]>([]);
  const [currentTime, setCurrentTime] = useState(0);
  const [message, setMessage] = useState("");
  const [isSubmitting, setSubmitting] = useState(false);

  const [providers, setProviders] = useState<ProviderSession[]>([]);
  const [cookiePlatform, setCookiePlatform] = useState<string | null>(null);

  const [discoverResults, setDiscoverResults] = useState<SourceVideo[] | null>(null);
  const [discovering, setDiscovering] = useState(false);

  const [reviewTab, setReviewTab] = useState<ReviewTab>("bilingual");
  const [selectedStage, setSelectedStage] = useState<string | null>(null);
  const [showAllFiles, setShowAllFiles] = useState(false);

  const refreshJobs = useCallback(async () => {
    const recent = await api.listJobs();
    setJobs(recent);
    setActiveJob((current) => current ?? recent[0] ?? null);
  }, []);

  useEffect(() => {
    refreshJobs().catch((error) => setMessage(error.message));
    api.loadProviders().then(setProviders).catch(() => undefined);
  }, [refreshJobs]);

  useEffect(() => {
    if (!activeJob) return;
    setCues([]);
    setGlossary([]);
    const done = ["succeeded", "failed", "canceled"].includes(activeJob.state);
    const hydrate = (job: JobManifest) => {
      api.loadCues(job.job_id).then(setCues).catch(() => setCues([]));
      api.loadGlossary(job.job_id).then(setGlossary).catch(() => setGlossary([]));
    };
    if (done) {
      hydrate(activeJob);
      return;
    }
    const events = new EventSource(`/api/v1/jobs/${activeJob.job_id}/events`);
    events.onmessage = (event) => {
      const job = JSON.parse(event.data) as JobManifest;
      setActiveJob(job);
      setJobs((items) => [job, ...items.filter((item) => item.job_id !== job.job_id)]);
      if (["succeeded", "failed", "canceled"].includes(job.state)) {
        events.close();
        hydrate(job);
      }
    };
    events.onerror = () => {
      events.close();
      api.fetchJob(activeJob.job_id).then(setActiveJob).catch(() => undefined);
    };
    return () => events.close();
  }, [activeJob?.job_id]);

  const stages = activeJob?.config?.stages?.length ? activeJob.config.stages : STAGES;
  const stageMap = useMemo(() => new Map(activeJob?.stages.map((stage) => [stage.name, stage])), [activeJob]);
  const progress = activeJob ? getProgress(activeJob, stages.length) : 0;
  const activeStageName = stages.find((name) => stageMap.get(name)?.status === "running");
  const currentCue = useMemo(() => findCue(cues, currentTime), [cues, currentTime]);
  const videoArtifact = activeJob ? latestArtifact(activeJob, "video") : undefined;
  const vttArtifact = activeJob ? latestArtifact(activeJob, "subtitle_vtt") : undefined;
  const voiceArtifact = activeJob ? latestArtifact(activeJob, "voice_track") : undefined;
  const qaStage = stageMap.get("qa");

  async function submitJob(event: FormEvent) {
    event.preventDefault();
    await runUrl(url.trim());
  }

  async function runUrl(target: string) {
    if (!target) return;
    setMessage("");
    setSubmitting(true);
    try {
      const job = await api.createJob(target);
      setUrl("");
      setDiscoverResults(null);
      setActiveJob(job);
      setJobs((items) => [job, ...items.filter((item) => item.job_id !== job.job_id)]);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không tạo được công việc");
    } finally {
      setSubmitting(false);
    }
  }

  async function runDiscover() {
    if (!url.trim()) return;
    setMessage("");
    setDiscovering(true);
    try {
      setDiscoverResults(await api.discoverVideos(url.trim()));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không quét được nguồn");
    } finally {
      setDiscovering(false);
    }
  }

  function selectStage(name: string) {
    setSelectedStage(name);
    setReviewTab("stage");
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
        <div className="deck-right">
          <ProviderBar providers={providers} onPick={setCookiePlatform} />
          <form className="source" onSubmit={submitJob}>
            <span className="source-tag">NGUỒN</span>
            <input
              aria-label="Đường dẫn video hoặc kênh nguồn"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="Dán link video, đoạn chia sẻ Douyin, hoặc link kênh rồi Quét…"
              required
              minLength={8}
            />
            <button type="button" className="ghost" onClick={runDiscover} disabled={discovering || !url.trim()}>
              {discovering ? "Đang quét…" : "Quét"}
            </button>
            <button disabled={isSubmitting || !url.trim()}>{isSubmitting ? "Đang dựng…" : "Dựng bản dub"}</button>
          </form>
        </div>
      </header>

      {message ? <p className="banner">{message}</p> : null}

      {discoverResults ? (
        <Intake videos={discoverResults} onRun={runUrl} onClose={() => setDiscoverResults(null)} />
      ) : null}

      <Transport
        stages={stages}
        stageMap={stageMap}
        state={activeJob?.state}
        progress={progress}
        activeStage={activeStageName}
        selected={selectedStage}
        onSelect={selectStage}
      />

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
                  src={`/media/${videoArtifact.path}`}
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

          {voiceArtifact ? (
            <div className="voicebar">
              <span className="voicebar-tag">GIỌNG ĐỌC</span>
              <audio controls src={`/media/${voiceArtifact.path}`} />
            </div>
          ) : null}

          <QaVerdict stage={qaStage} />
        </section>

        <aside className="review panel">
          <div className="tabbar">
            <button className={reviewTab === "bilingual" ? "tab on" : "tab"} onClick={() => setReviewTab("bilingual")}>
              Song ngữ <span className="count">{cues.length || ""}</span>
            </button>
            <button className={reviewTab === "glossary" ? "tab on" : "tab"} onClick={() => setReviewTab("glossary")}>
              Thuật ngữ <span className="count">{glossary.length || ""}</span>
            </button>
            <button className={reviewTab === "stage" ? "tab on" : "tab"} onClick={() => setReviewTab("stage")}>
              Chi tiết
            </button>
          </div>

          <div className="review-body">
            {reviewTab === "bilingual" ? (
              <Bilingual cues={cues} current={currentCue} onSeek={(t) => seekVideo(videoRef.current, t)} />
            ) : null}
            {reviewTab === "glossary" ? <GlossaryTable entries={glossary} /> : null}
            {reviewTab === "stage" ? <StageInspector stage={selectedStage ? stageMap.get(selectedStage) : undefined} name={selectedStage} /> : null}
          </div>

          <Downloads job={activeJob} showAll={showAllFiles} onToggle={() => setShowAllFiles((value) => !value)} />
        </aside>
      </main>

      <Ledger jobs={jobs} activeId={activeJob?.job_id} onPick={setActiveJob} onRefresh={() => refreshJobs().catch((error) => setMessage(error.message))} />

      {cookiePlatform ? (
        <CookieDialog
          session={providers.find((session) => session.platform === cookiePlatform)}
          onClose={() => setCookiePlatform(null)}
          onSaved={() => api.loadProviders().then(setProviders).catch(() => undefined)}
        />
      ) : null}
    </div>
  );
}

/* ---------- transport ---------- */

function Transport({
  stages,
  stageMap,
  state,
  progress,
  activeStage,
  selected,
  onSelect,
}: {
  stages: string[];
  stageMap: Map<string, Stage>;
  state?: JobState;
  progress: number;
  activeStage?: string;
  selected: string | null;
  onSelect: (name: string) => void;
}) {
  const headline = activeStage
    ? STAGE_LABELS[activeStage]?.title ?? activeStage
    : state === "succeeded"
      ? "Hoàn tất"
      : state === "failed"
        ? "Dừng do lỗi"
        : "Chờ chạy";
  return (
    <section className={`transport ${state === "running" ? "live" : ""}`}>
      <div className="transport-head">
        <span className="transport-tag">TIẾN TRÌNH</span>
        <span className="transport-now">{headline}</span>
        <span className="transport-pct mono">{progress.toString().padStart(2, "0")}%</span>
      </div>
      <ol className="pipeline">
        {stages.map((name, index) => {
          const status = stageMap.get(name)?.status ?? "pending";
          const label = STAGE_LABELS[name] ?? { title: name, detail: "" };
          return (
            <li key={name} className="seg-wrap">
              <button
                className={`seg ${status} ${selected === name ? "picked" : ""}`}
                onClick={() => onSelect(name)}
                title={`Xem chi tiết: ${label.title}`}
              >
                <span className="seg-top">
                  <span className="seg-num mono">{String(index + 1).padStart(2, "0")}</span>
                  <StageGlyph status={status} />
                </span>
                <span className="seg-name">{label.title}</span>
                <span className="seg-detail">{label.detail}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function StageGlyph({ status }: { status: StageStatus }) {
  if (status === "succeeded") return <span className="seg-glyph done" aria-hidden>✓</span>;
  if (status === "failed") return <span className="seg-glyph fail" aria-hidden>!</span>;
  if (status === "running") return <span className="seg-glyph run" aria-hidden />;
  return <span className="seg-glyph idle" aria-hidden />;
}

/* ---------- review: bilingual ---------- */

function Bilingual({ cues, current, onSeek }: { cues: SubtitleCue[]; current?: SubtitleCue; onSeek: (time?: number) => void }) {
  if (!cues.length) {
    return <p className="muted-note">Bảng đối chiếu hiện khi bước Kiểm tra hoàn tất. Mỗi dòng ghép câu nguồn 中文 với lời Việt.</p>;
  }
  return (
    <div className="cues">
      {cues.map((cue, index) => (
        <button key={`${cue.start}-${index}`} className={`cue ${current === cue ? "on" : ""}`} onClick={() => onSeek(cue.start)}>
          <time className="mono">{formatTime(cue.start)}</time>
          <div className="cue-pair">
            {cue.source ? <p className="cue-zh">{cue.source}</p> : null}
            <p className="cue-vi">{cue.translated || cue.text || "—"}</p>
          </div>
        </button>
      ))}
    </div>
  );
}

/* ---------- review: glossary ---------- */

function GlossaryTable({ entries }: { entries: GlossaryEntry[] }) {
  if (!entries.length) {
    return <p className="muted-note">Thuật ngữ trích tự động từ video (tên riêng, cấp bậc, số liệu) sẽ liệt kê ở đây để soát tính nhất quán.</p>;
  }
  return (
    <div className="glossary">
      {entries.map((entry, index) => (
        <div key={`${entry.term_zh}-${index}`} className="term">
          <span className="term-zh">{entry.term_zh}</span>
          <span className="term-arrow" aria-hidden>→</span>
          <span className="term-vi">{entry.term_vi}</span>
          {entry.kind ? <span className="term-kind">{entry.kind}</span> : null}
          {entry.note ? <span className="term-note">{entry.note}</span> : null}
        </div>
      ))}
    </div>
  );
}

/* ---------- review: stage inspector ---------- */

function StageInspector({ stage, name }: { stage?: Stage; name: string | null }) {
  if (!name) return <p className="muted-note">Bấm một bước trên thanh tiến trình để xem số liệu, log và lỗi của bước đó.</p>;
  const label = STAGE_LABELS[name]?.title ?? name;
  if (!stage) return <p className="muted-note">Bước “{label}” chưa chạy.</p>;
  const metrics = Object.entries(stage.metrics ?? {});
  return (
    <div className="inspector">
      <div className="inspector-head">
        <h3>{label}</h3>
        <span className={`chip-state ${stage.status}`}>{stage.status}</span>
      </div>
      {stage.error ? <p className="fault">{stage.error}</p> : null}
      {metrics.length ? (
        <dl className="metrics">
          {metrics.map(([key, value]) => (
            <div key={key} className="metric">
              <dt className="mono">{key}</dt>
              <dd className="mono">{renderMetric(value)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="muted-note">Không có số liệu.</p>
      )}
      {stage.logs?.length ? (
        <details className="logs">
          <summary>Log ({stage.logs.length})</summary>
          <pre className="mono">{stage.logs.join("\n")}</pre>
        </details>
      ) : null}
    </div>
  );
}

/* ---------- QA verdict ---------- */

function QaVerdict({ stage }: { stage?: Stage }) {
  if (!stage || stage.status === "pending" || stage.status === "running") return null;
  const issues = Number(stage.metrics?.issues ?? 0);
  const coverage = (stage.metrics?.coverage ?? {}) as Record<string, number>;
  const passed = stage.status === "succeeded" && issues === 0;
  return (
    <div className={`verdict ${passed ? "pass" : "fail"}`}>
      <span className="verdict-mark" aria-hidden>{passed ? "✓" : "!"}</span>
      <div>
        <strong>{passed ? "Đạt kiểm tra" : "Chưa đạt kiểm tra"}</strong>
        <p className="mono small">
          missing {coverage.missing ?? 0} · overlap {coverage.overlaps ?? 0} · Hán {coverage.contains_chinese ?? 0} · rỗng {coverage.empty ?? 0}
        </p>
      </div>
    </div>
  );
}

/* ---------- downloads ---------- */

const PRIMARY: [string, string][] = [
  ["dubbed_mp4", "MP4 lồng tiếng"],
  ["soft_sub_mp4", "MP4 phụ đề"],
  ["subtitle_srt", "Phụ đề SRT"],
];
const SECONDARY: [string, string][] = [
  ["voice_track", "Giọng đọc"],
  ["subtitle_vtt", "VTT"],
  ["subtitle_json", "JSON"],
  ["qa_report", "Báo cáo QA"],
  ["glossary", "Thuật ngữ"],
  ["zip_package", "Gói ZIP"],
];

function Downloads({ job, showAll, onToggle }: { job: JobManifest | null; showAll: boolean; onToggle: () => void }) {
  if (!job) return null;
  const primary = PRIMARY.filter(([kind]) => latestArtifact(job, kind));
  return (
    <div className="outputs">
      <span className="outputs-tag">XUẤT FILE</span>
      <div className="chips">
        {primary.length ? (
          primary.map(([kind, label]) => (
            <a key={kind} className="chip ready primary" href={`/api/v1/jobs/${job.job_id}/artifacts/${kind}`}>
              {label}
            </a>
          ))
        ) : (
          <span className="muted-note tiny">File xuất hiện khi đóng gói xong.</span>
        )}
        {showAll
          ? SECONDARY.filter(([kind]) => latestArtifact(job, kind)).map(([kind, label]) => (
              <a key={kind} className="chip ready" href={`/api/v1/jobs/${job.job_id}/artifacts/${kind}`}>
                {label}
              </a>
            ))
          : null}
      </div>
      <button className="link tiny" onClick={onToggle}>
        {showAll ? "Ẩn file phụ" : "Tất cả file"}
      </button>
    </div>
  );
}

/* ---------- provider sessions ---------- */

function ProviderBar({ providers, onPick }: { providers: ProviderSession[]; onPick: (platform: string) => void }) {
  if (!providers.length) return null;
  return (
    <div className="providers">
      {providers.map((session) => (
        <button
          key={session.platform}
          className={`prov ${session.configured ? "ok" : "off"}`}
          onClick={() => onPick(session.platform)}
          title={session.note}
        >
          <span className="prov-dot" aria-hidden />
          {session.label}
          <span className="prov-state">{session.configured ? "phiên ✓" : "cần cookie"}</span>
        </button>
      ))}
    </div>
  );
}

function CookieDialog({ session, onClose, onSaved }: { session?: ProviderSession; onClose: () => void; onSaved: () => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!session) return null;
  async function save() {
    setBusy(true);
    setError("");
    try {
      await api.saveCookies(session!.platform, value);
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không lưu được cookie");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h3>Phiên {session.label}</h3>
        <p className="muted-note">{session.note}</p>
        {session.required_missing.length ? (
          <p className="mono small">Cần: {session.required_missing.join(", ")}</p>
        ) : null}
        <textarea
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="Dán cookie dạng Netscape hoặc header Cookie từ trình duyệt…"
          rows={6}
        />
        {error ? <p className="fault">{error}</p> : null}
        <div className="modal-actions">
          <button className="ghost" onClick={onClose}>
            Đóng
          </button>
          <button onClick={save} disabled={busy || value.trim().length < 4}>
            {busy ? "Đang lưu…" : "Lưu phiên"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ---------- intake (discover) ---------- */

function Intake({ videos, onRun, onClose }: { videos: SourceVideo[]; onRun: (url: string) => void; onClose: () => void }) {
  return (
    <section className="intake panel">
      <div className="intake-head">
        <span className="intake-tag">ĐÃ QUÉT · {videos.length} VIDEO</span>
        <button className="link" onClick={onClose}>
          Ẩn
        </button>
      </div>
      {videos.length ? (
        <div className="intake-grid">
          {videos.map((video) => (
            <article key={video.id} className="vcard">
              <div className="vthumb">{video.thumbnail_url ? <img src={video.thumbnail_url} alt="" loading="lazy" /> : <span className="reel" aria-hidden />}</div>
              <div className="vmeta">
                <p className="vtitle">{video.title || video.platform_video_id}</p>
                <p className="mono tiny">
                  {video.platform}
                  {video.duration ? ` · ${formatTime(video.duration)}` : ""}
                  {video.uploader ? ` · ${video.uploader}` : ""}
                </p>
                {video.error ? <p className="vnote">{video.error}</p> : null}
              </div>
              <button className="ghost tiny" onClick={() => onRun(video.source_url)}>
                Dựng
              </button>
            </article>
          ))}
        </div>
      ) : (
        <p className="muted-note">Không tìm thấy video trong nguồn này.</p>
      )}
    </section>
  );
}

/* ---------- ledger ---------- */

function Ledger({ jobs, activeId, onPick, onRefresh }: { jobs: JobManifest[]; activeId?: string; onPick: (job: JobManifest) => void; onRefresh: () => void }) {
  return (
    <footer className="ledger panel">
      <div className="ledger-head">
        <span className="ledger-tag">CÔNG VIỆC GẦN ĐÂY</span>
        <button className="link" onClick={onRefresh}>
          Làm mới
        </button>
      </div>
      <div className="ledger-row">
        {jobs.length ? (
          jobs.map((job) => (
            <button key={job.job_id} className={`ticket ${activeId === job.job_id ? "active" : ""}`} onClick={() => onPick(job)}>
              <span className="mono ticket-id">{job.job_id}</span>
              <span className={`ticket-state ${job.state}`}>{STATE_VI[job.state]}</span>
            </button>
          ))
        ) : (
          <p className="muted-note">Chưa có công việc nào. Dán một link để bắt đầu.</p>
        )}
      </div>
    </footer>
  );
}

/* ---------- bits + helpers ---------- */

function StatePill({ state }: { state: JobState }) {
  return (
    <span className={`pill ${state}`}>
      <span className="pill-dot" aria-hidden />
      {STATE_VI[state]}
    </span>
  );
}

function renderMetric(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function latestArtifact(job: JobManifest, kind: string) {
  return [...job.artifacts].reverse().find((artifact) => artifact.kind === kind);
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
