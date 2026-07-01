import { useEffect, useState } from "react";
import * as api from "../api";
import type { Episode, JobManifest } from "../types";
import { STAGE_VI, dubErrorVi, latestArtifact } from "../util";

const STAGE_ORDER = ["download", "asr", "glossary", "translate", "edit", "qa", "voiceover", "package"];

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
 *  local video (original/dubbed toggle); a not-yet-dubbed one shows the cover with a
 *  link to the source and a Dịch button (so there is always something to preview). */
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

  useEffect(() => {
    if (!episode.dub_job_id) return;
    setJob(null);
    api.fetchJob(episode.dub_job_id).then(setJob).catch((err) => setError(String(err)));
  }, [episode.dub_job_id]);

  const dubArt = job ? latestArtifact(job, "hardsub_mp4") ?? latestArtifact(job, "dubbed_mp4") : undefined;
  const srcArt = job ? latestArtifact(job, "video") : undefined;
  const art = dubbed ? dubArt ?? srcArt : srcArt;
  const isDubbed = episode.dub_state === "dubbed";

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

        <div className="pv-screen">
          {isDubbed && art ? (
            <video key={art.path} controls autoPlay playsInline src={`/media/${art.path}`} />
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

        <div className="pv-actions">
          {isDubbed && dubArt ? (
            <div className="seg-toggle">
              <button className={dubbed ? "" : "on"} onClick={() => setDubbed(false)}>Gốc</button>
              <button className={dubbed ? "on" : ""} onClick={() => setDubbed(true)}>Lồng tiếng</button>
            </div>
          ) : (
            <a className="ghost" href={episode.source_url} target="_blank" rel="noreferrer">Xem gốc trên nền tảng ↗</a>
          )}
          <div className="pv-right">
            <button className="primary" onClick={onRedub}>{isDubbed ? "Làm lại" : "Dịch tập này"}</button>
            {isDubbed ? <button className="ghost danger" onClick={onDelete}>Xóa bản dịch</button> : null}
          </div>
        </div>
      </div>
    </div>
  );
}
