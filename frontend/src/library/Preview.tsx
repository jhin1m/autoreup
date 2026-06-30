import { useEffect, useState } from "react";
import * as api from "../api";
import type { Episode, JobManifest } from "../types";
import { latestArtifact } from "../util";

/** Watch one dubbed episode — the only "studio" surface left. Plays the dubbed video,
 *  toggles original vs dubbed, and offers re-dub / delete. No stage/glossary internals. */
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
    api.fetchJob(episode.dub_job_id).then(setJob).catch((err) => setError(String(err)));
  }, [episode.dub_job_id]);

  const dubArt = job ? latestArtifact(job, "hardsub_mp4") ?? latestArtifact(job, "dubbed_mp4") : undefined;
  const srcArt = job ? latestArtifact(job, "video") : undefined;
  const art = dubbed ? dubArt ?? srcArt : srcArt;

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="preview-modal" onClick={(event) => event.stopPropagation()}>
        <div className="pv-head">
          <p className="pv-title">{episode.title}</p>
          <button className="link" onClick={onClose}>Đóng</button>
        </div>

        <div className="pv-screen">
          {art ? (
            <video key={art.path} controls autoPlay playsInline src={`/media/${art.path}`} />
          ) : (
            <div className="pv-empty">
              <span className="reel" aria-hidden />
              <p>{error || (episode.dub_job_id ? "Đang tải video…" : "Tập này chưa được lồng tiếng.")}</p>
            </div>
          )}
        </div>

        <div className="pv-actions">
          {dubArt ? (
            <div className="seg-toggle">
              <button className={dubbed ? "" : "on"} onClick={() => setDubbed(false)}>Gốc</button>
              <button className={dubbed ? "on" : ""} onClick={() => setDubbed(true)}>Lồng tiếng</button>
            </div>
          ) : <span />}
          <div className="pv-right">
            <button className="ghost" onClick={onRedub}>Làm lại</button>
            <button className="ghost danger" onClick={onDelete}>Xóa bản dịch</button>
          </div>
        </div>
      </div>
    </div>
  );
}
