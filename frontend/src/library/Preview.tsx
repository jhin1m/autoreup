import { useEffect, useRef, useState } from "react";
import * as api from "../api";
import type { Artifact, Episode, JobManifest, SubtitleCue } from "../types";
import { STAGE_VI, dubErrorVi, formatDuration, latestArtifact } from "../util";

const STAGE_ORDER = ["download", "asr", "glossary", "translate", "qa", "voiceover", "package"];

type Cue = { start: number; end: number; text: string };

function toCues(rows: SubtitleCue[]): Cue[] {
  return rows
    .filter((row): row is SubtitleCue & { start: number; end: number } =>
      typeof row.start === "number" && typeof row.end === "number")
    .map((row) => ({ start: row.start, end: row.end, text: (row.text ?? row.translated ?? "").trim() }))
    .filter((cue) => cue.text.length > 0);
}

/** Pipeline checklist so a translating/failed episode shows how far it got and where. */
function StageProgress({ ep }: { ep: Episode }) {
  const done = ep.dub_stage_index ?? 0;
  const failed = ep.dub_state === "failed";
  return (
    <div className="pv-stages">
      <ol>
        {STAGE_ORDER.map((name, i) => {
          const state = failed && i === done - 1 ? "fail" : i < done ? "done" : i === done && !failed ? "now" : "todo";
          return (
            <li key={name} className={`stg ${state}`}>
              <span className="stg-mark" aria-hidden>{state === "done" ? "✓" : state === "fail" ? "✕" : state === "now" ? "●" : "○"}</span>
              {STAGE_VI[name] ?? name}
            </li>
          );
        })}
      </ol>
      {failed && ep.dub_error ? <p className="pv-err" title={ep.dub_error}>⚠ {dubErrorVi(ep.dub_error)}</p> : null}
    </div>
  );
}

/** Watch/inspect one episode — the only "studio" surface. A dubbed episode plays its
 *  local video (original/dubbed toggle) next to a synced subtitle list: the active cue
 *  is highlighted and kept in view, clicking a row seeks the player. */
export function Preview({
  episode,
  onClose,
  onRedub,
  onDelete,
}: {
  episode: Episode;
  onClose: () => void;
  onRedub: () => void;
  onDelete: () => void;
}) {
  const [job, setJob] = useState<JobManifest | null>(null);
  const [dubbed, setDubbed] = useState(true);
  const [error, setError] = useState("");
  const [cues, setCues] = useState<Cue[]>([]);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [artIndex, setArtIndex] = useState(0);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const activeRowRef = useRef<HTMLLIElement | null>(null);

  useEffect(() => {
    if (!episode.dub_job_id) return;
    setJob(null);
    setCues([]);
    setActiveIndex(-1);
    api.fetchJob(episode.dub_job_id).then(setJob).catch((err) => setError(String(err)));
  }, [episode.dub_job_id]);

  useEffect(() => {
    setArtIndex(0);
  }, [episode.dub_job_id, job, dubbed]);

  // Load the final subtitle track once the manifest is known (served from /media).
  useEffect(() => {
    const artifact = job ? latestArtifact(job, "subtitle_json") : undefined;
    if (!artifact) return;
    let cancelled = false;
    fetch(`/media/${artifact.path}`)
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
      .then((rows: SubtitleCue[]) => {
        if (!cancelled) setCues(toCues(rows));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [job]);

  useEffect(() => {
    activeRowRef.current?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  const dubArt = job ? latestArtifact(job, "hardsub_mp4") ?? latestArtifact(job, "dubbed_mp4") : undefined;
  const srcArt = job ? latestArtifact(job, "video") : undefined;
  // Prefer the dubbed render, but fall back to the original if that file is gone
  // (a stale manifest artifact is dropped server-side; a broken file may still 404).
  const candidates = (dubbed ? [dubArt, srcArt] : [srcArt]).filter((artifact): artifact is Artifact => Boolean(artifact));
  const art = candidates[Math.min(artIndex, Math.max(0, candidates.length - 1))];
  const isDubbed = episode.dub_state === "dubbed";
  const fellBackToSource = dubbed && !!dubArt && artIndex > 0;
  const showSubs = isDubbed && dubbed && !!dubArt && cues.length > 0 && !fellBackToSource;

  function handleTimeUpdate(event: React.SyntheticEvent<HTMLVideoElement>) {
    const time = event.currentTarget.currentTime;
    let index = -1;
    for (let i = 0; i < cues.length; i += 1) {
      const cue = cues[i]!;
      if (cue.start > time) break;
      if (time < cue.end) {
        index = i;
        break;
      }
    }
    if (index !== activeIndex) setActiveIndex(index);
  }

  function seekTo(cue: Cue) {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = cue.start;
    void video.play();
  }

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="preview-modal" onClick={(event) => event.stopPropagation()}>
        <div className="pv-head">
          <div className="pv-titles">
            <p className="pv-title">{episode.title_vi || episode.title}</p>
            {episode.title_vi ? <p className="pv-title-zh">{episode.title}</p> : null}
          </div>
          <button className="link" onClick={onClose}>Đóng</button>
        </div>

        <div className={`pv-body ${showSubs ? "with-subs" : ""}`}>
          <div className="pv-screen">
            {isDubbed && art ? (
              <video
                key={art.path}
                ref={videoRef}
                controls
                autoPlay
                playsInline
                src={`/media/${art.path}`}
                onTimeUpdate={handleTimeUpdate}
                onError={() => setArtIndex((index) => (index + 1 < candidates.length ? index + 1 : index))}
              />
            ) : (
              <div className="pv-empty">
                {episode.dub_state === "translating" || episode.dub_state === "failed" ? (
                  <StageProgress ep={episode} />
                ) : (
                  <>
                    {episode.thumbnail_url ? <img className="pv-cover" src={`/api/v1/library/thumb?url=${encodeURIComponent(episode.thumbnail_url)}`} alt="" /> : <span className="reel" aria-hidden />}
                    <p>{error ? "Không tải được video." : "Tập này chưa lồng tiếng. Xem bản gốc rồi bấm Dịch."}</p>
                  </>
                )}
              </div>
            )}
          </div>

          {showSubs ? (
            <aside className="pv-subs" aria-label="Danh sách phụ đề">
              <p className="pv-subs-head mono">{cues.length} dòng phụ đề</p>
              <ol>
                {cues.map((cue, index) => (
                  <li
                    key={index}
                    ref={index === activeIndex ? activeRowRef : undefined}
                    className={index === activeIndex ? "on" : ""}
                    aria-current={index === activeIndex ? "true" : undefined}
                  >
                    <button onClick={() => seekTo(cue)} title="Nhảy tới dòng này">
                      <span className="mono tiny">{formatDuration(cue.start)}</span>
                      <span className="pv-sub-text">{cue.text}</span>
                    </button>
                  </li>
                ))}
              </ol>
            </aside>
          ) : null}
        </div>

        <div className="pv-actions">
          {isDubbed && dubArt ? (
            <div className="seg-toggle">
              <button className={dubbed ? "" : "on"} onClick={() => setDubbed(false)}>Gốc</button>
              <button className={dubbed ? "on" : ""} onClick={() => setDubbed(true)}>Lồng tiếng</button>
            </div>
          ) : (
            <a className="ghost" href={episode.source_url} target="_blank" rel="noreferrer">Xem gốc trên nền tảng ↗</a>
          )}
          {fellBackToSource ? <span className="mono tiny amber">Thiếu file lồng tiếng — đang phát bản gốc</span> : null}
          <div className="pv-right">
            <button className="primary" onClick={onRedub}>{isDubbed ? "Làm lại" : "Dịch tập này"}</button>
            {isDubbed ? <button className="ghost danger" onClick={onDelete}>Xóa bản dịch</button> : null}
          </div>
        </div>
      </div>
    </div>
  );
}
