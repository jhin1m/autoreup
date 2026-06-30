import { useState } from "react";
import { useProviders, useSaveCookies } from "./queries";
import { useToast } from "./toast";
import type { ProviderSession } from "./types";

/** Settings: provider sessions (cookie status + import). The home for cookie management
 *  that used to float in the header. */
export function Settings({ onClose }: { onClose: () => void }) {
  const { data: providers = [] } = useProviders();
  const [editing, setEditing] = useState<ProviderSession | null>(null);

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <h3>Cài đặt · Phiên nền tảng</h3>
        {editing ? (
          <CookieEditor session={editing} onBack={() => setEditing(null)} />
        ) : (
          <div className="settings-list">
            {providers.map((session) => (
              <button key={session.platform} className={`settings-row ${session.configured ? "ok" : "off"}`} onClick={() => setEditing(session)}>
                <span className="prov-dot" aria-hidden />
                <span className="settings-name">{session.label}</span>
                <span className="settings-state">{session.configured ? "phiên ✓" : "cần cookie"}</span>
              </button>
            ))}
            {!providers.length ? <p className="muted-note">Chưa có nền tảng nào.</p> : null}
          </div>
        )}
        {!editing ? (
          <div className="modal-actions">
            <button className="ghost" onClick={onClose}>Đóng</button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function CookieEditor({ session, onBack }: { session: ProviderSession; onBack: () => void }) {
  const [value, setValue] = useState("");
  const save = useSaveCookies();
  const { toast } = useToast();

  async function submit() {
    try {
      await save.mutateAsync({ platform: session.platform, cookies: value });
      toast(`Đã lưu phiên ${session.label}`, "success");
      onBack();
    } catch (error) {
      toast(error instanceof Error ? error.message : "Không lưu được cookie", "error");
    }
  }

  return (
    <>
      <p className="muted-note">{session.note}</p>
      {session.required_missing.length ? <p className="mono small">Cần: {session.required_missing.join(", ")}</p> : null}
      <textarea
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder='Dán cookie JSON từ Cookie-Editor ({"cookies":[…]}), file cookies.txt, hoặc header Cookie…'
        rows={6}
      />
      <div className="modal-actions">
        <button className="ghost" onClick={onBack}>Quay lại</button>
        <button className="primary" onClick={submit} disabled={save.isPending || value.trim().length < 4}>
          {save.isPending ? "Đang lưu…" : "Lưu phiên"}
        </button>
      </div>
    </>
  );
}
