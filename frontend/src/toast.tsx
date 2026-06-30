import { createContext, ReactNode, useCallback, useContext, useMemo, useState } from "react";

type ToastKind = "info" | "success" | "error";
type Toast = { id: number; kind: ToastKind; message: string };
type ConfirmState = { message: string; resolve: (ok: boolean) => void } | null;

type ToastApi = {
  toast: (message: string, kind?: ToastKind) => void;
  confirm: (message: string) => Promise<boolean>;
};

const ToastContext = createContext<ToastApi | null>(null);

let nextId = 1;

/** App-wide notifications + a promise-based confirm dialog — the one place transient
 *  feedback and destructive confirmations live (replaces window.confirm + ad-hoc strings). */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirmState, setConfirmState] = useState<ConfirmState>(null);

  const toast = useCallback((message: string, kind: ToastKind = "info") => {
    const id = nextId++;
    setToasts((list) => [...list, { id, kind, message }]);
    setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), 4000);
  }, []);

  const confirm = useCallback(
    (message: string) => new Promise<boolean>((resolve) => setConfirmState({ message, resolve })),
    [],
  );

  const settle = (ok: boolean) => {
    confirmState?.resolve(ok);
    setConfirmState(null);
  };

  const api = useMemo(() => ({ toast, confirm }), [toast, confirm]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-stack">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>{t.message}</div>
        ))}
      </div>
      {confirmState ? (
        <div className="modal-scrim" onClick={() => settle(false)}>
          <div className="confirm-dialog" onClick={(event) => event.stopPropagation()}>
            <p className="confirm-msg">{confirmState.message}</p>
            <div className="modal-actions">
              <button className="ghost" onClick={() => settle(false)}>Huỷ</button>
              <button className="primary danger-btn" onClick={() => settle(true)}>Xác nhận</button>
            </div>
          </div>
        </div>
      ) : null}
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
