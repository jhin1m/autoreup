import { useState } from "react";
import * as api from "../api";
import type { ChannelGroup } from "../types";

/** Paste a channel link → auto-cluster its videos into series ("bộ") → review/rename →
 *  create. A channel mixes several shows, so each detected group becomes its own series. */
export function ChannelImport({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const [url, setUrl] = useState("");
  const [groups, setGroups] = useState<ChannelGroup[] | null>(null);
  const [titles, setTitles] = useState<string[]>([]);
  const [skip, setSkip] = useState<Set<number>>(new Set());
  const [phase, setPhase] = useState<"idle" | "scanning" | "importing">("idle");
  const [message, setMessage] = useState("");

  async function scan() {
    if (!url.trim()) return;
    setPhase("scanning");
    setMessage("");
    try {
      const result = await api.previewChannel(url.trim());
      setGroups(result);
      setTitles(result.map((g) => g.title));
      setSkip(new Set());
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không quét được kênh");
    } finally {
      setPhase("idle");
    }
  }

  async function importChosen() {
    if (!groups) return;
    const chosen = groups
      .map((g, index) => ({ title: titles[index]?.trim() || g.title, episodes: g.episodes }))
      .filter((_, index) => !skip.has(index));
    if (!chosen.length) return;
    setPhase("importing");
    setMessage("");
    try {
      await api.importChannel(url.trim(), chosen);
      onImported();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không tạo được bộ");
      setPhase("idle");
    }
  }

  const toggleSkip = (index: number) =>
    setSkip((prev) => {
      const next = new Set(prev);
      next.has(index) ? next.delete(index) : next.add(index);
      return next;
    });
  const chosenCount = groups ? groups.length - skip.size : 0;

  return (
    <div className="channel-import panel">
      <div className="ci-head">
        <span className="ci-tag mono">QUÉT KÊNH → TỰ TÁCH BỘ</span>
        <button className="link" onClick={onClose}>Đóng</button>
      </div>
      <div className="ci-scan">
        <input
          className="lib-input"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="Dán link kênh (1 kênh nhiều bộ — hệ thống tự tách)…"
          autoFocus
        />
        <button className="primary" disabled={phase === "scanning" || !url.trim()} onClick={scan}>
          {phase === "scanning" ? "Đang quét…" : "Quét"}
        </button>
      </div>

      {message ? <p className="lib-msg">{message}</p> : null}

      {groups ? (
        groups.length ? (
          <>
            <p className="ci-hint">Tìm thấy {groups.length} bộ. Sửa tên, bỏ tick bộ không muốn, rồi tạo.</p>
            <div className="ci-groups">
              {groups.map((group, index) => (
                <div key={index} className={`ci-group ${skip.has(index) ? "skip" : ""}`}>
                  <input type="checkbox" checked={!skip.has(index)} onChange={() => toggleSkip(index)} />
                  <input
                    className="ci-title-input"
                    value={titles[index] ?? ""}
                    onChange={(event) => setTitles((prev) => prev.map((t, i) => (i === index ? event.target.value : t)))}
                  />
                  <span className="ci-count mono">{group.episodes.length} tập</span>
                  <span className="ci-sample">{String((group.episodes[0] as { title?: string })?.title ?? "").slice(0, 30)}…</span>
                </div>
              ))}
            </div>
            <button className="primary" disabled={!chosenCount || phase === "importing"} onClick={importChosen}>
              {phase === "importing" ? "Đang tạo…" : `Tạo ${chosenCount} bộ`}
            </button>
          </>
        ) : (
          <p className="muted-note">Không tách được bộ nào từ kênh này.</p>
        )
      ) : null}
    </div>
  );
}
