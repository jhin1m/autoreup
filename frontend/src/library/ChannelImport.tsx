import { useEffect, useState } from "react";
import * as api from "../api";
import { useImportChannel } from "../queries";
import { useToast } from "../toast";
import type { ChannelGroup } from "../types";

/** Channel → series. Given a channel URL, auto-cluster its videos into shows; existing
 *  series are matched (sync, not duplicate). User reviews names / unticks, then imports. */
export function ChannelImport({ url, onDone }: { url: string; onDone: () => void }) {
  const [groups, setGroups] = useState<ChannelGroup[] | null>(null);
  const [titles, setTitles] = useState<string[]>([]);
  const [skip, setSkip] = useState<Set<number>>(new Set());
  const [scanning, setScanning] = useState(true);
  const importChannel = useImportChannel();
  const { toast } = useToast();

  useEffect(() => {
    let alive = true;
    api
      .previewChannel(url)
      .then((result) => {
        if (!alive) return;
        setGroups(result);
        setTitles(result.map((g) => g.title));
        setSkip(new Set(result.map((g, i) => (g.series_id && g.new_count === 0 ? i : -1)).filter((i) => i >= 0)));
      })
      .catch((error) => alive && toast(error instanceof Error ? error.message : "Không quét được kênh", "error"))
      .finally(() => alive && setScanning(false));
    return () => {
      alive = false;
    };
  }, [url, toast]);

  async function importChosen() {
    if (!groups) return;
    const chosen = groups
      .map((g, index) => ({ title: titles[index]?.trim() || g.title, series_id: g.series_id, episodes: g.episodes }))
      .filter((_, index) => !skip.has(index));
    if (!chosen.length) return;
    try {
      const ids = await importChannel.mutateAsync({ url, groups: chosen });
      toast(`Đã đồng bộ ${ids.length} bộ`, "success");
      onDone();
    } catch (error) {
      toast(error instanceof Error ? error.message : "Không tạo được bộ", "error");
    }
  }

  const toggleSkip = (index: number) =>
    setSkip((prev) => {
      const next = new Set(prev);
      next.has(index) ? next.delete(index) : next.add(index);
      return next;
    });
  const chosenCount = groups ? groups.length - skip.size : 0;

  if (scanning) return <p className="ci-hint">Đang quét kênh…</p>;
  if (!groups || !groups.length) return <p className="muted-note">Không tách được bộ nào từ kênh này.</p>;

  return (
    <>
      <p className="ci-hint">Tìm thấy {groups.length} bộ. Bộ đã có sẽ được bổ sung tập mới; bộ mới sẽ được tạo.</p>
      <div className="ci-groups">
        {groups.map((group, index) => (
          <div key={index} className={`ci-group ${skip.has(index) ? "skip" : ""}`}>
            <input type="checkbox" checked={!skip.has(index)} onChange={() => toggleSkip(index)} />
            <input
              className="ci-title-input"
              value={titles[index] ?? ""}
              onChange={(event) => setTitles((prev) => prev.map((t, i) => (i === index ? event.target.value : t)))}
            />
            {group.series_id ? (
              <span className={`ci-badge ${group.new_count ? "new" : "same"}`}>
                {group.new_count ? `đã có · +${group.new_count} tập mới` : "đã có · không mới"}
              </span>
            ) : (
              <span className="ci-badge fresh">mới · {group.total} tập</span>
            )}
            <span className="ci-sample">{String((group.episodes[0] as { title?: string })?.title ?? "").slice(0, 28)}…</span>
          </div>
        ))}
      </div>
      <button className="primary" disabled={!chosenCount || importChannel.isPending} onClick={importChosen}>
        {importChannel.isPending ? "Đang đồng bộ…" : `Đồng bộ ${chosenCount} bộ`}
      </button>
    </>
  );
}
