import { useEffect, useRef, useState } from "react";
import { CheckCircle2, AlertTriangle, Info, XCircle, X } from "lucide-react";

/** 全局轻提示: 任何模块 import { toast } 调用, 不阻塞操作(替代 window.alert)。 */
let _push = null;

export function toast(message, type = "info", duration = 3200) {
  if (_push) _push({ message: String(message ?? ""), type, duration });
  else console.log(`[toast:${type}]`, message);
}

export const toastOk = (m) => toast(m, "success");
export const toastErr = (m) => toast(m, "error", 5000);
export const toastWarn = (m) => toast(m, "warning", 4200);

const STYLE = {
  success: { icon: CheckCircle2, cls: "border-green-200 bg-green-50 text-green-800", ic: "text-green-600" },
  error: { icon: XCircle, cls: "border-red-200 bg-red-50 text-red-800", ic: "text-red-600" },
  warning: { icon: AlertTriangle, cls: "border-amber-200 bg-amber-50 text-amber-800", ic: "text-amber-600" },
  info: { icon: Info, cls: "border-brand-200 bg-brand-50 text-brand-700", ic: "text-brand-600" },
};

export function ToastHost() {
  const [items, setItems] = useState([]);
  const idRef = useRef(0);

  useEffect(() => {
    _push = ({ message, type, duration }) => {
      const id = ++idRef.current;
      setItems((prev) => [...prev.slice(-4), { id, message, type }]);
      setTimeout(() => setItems((prev) => prev.filter((x) => x.id !== id)), duration);
    };
    return () => { _push = null; };
  }, []);

  if (!items.length) return null;
  return (
    <div className="fixed top-3 left-1/2 -translate-x-1/2 z-[100] space-y-2 w-[calc(100%-2rem)] max-w-md pointer-events-none">
      {items.map((t) => {
        const s = STYLE[t.type] || STYLE.info;
        const Icon = s.icon;
        return (
          <div key={t.id}
            className={`pointer-events-auto flex items-start gap-2 border rounded-xl px-3 py-2.5 shadow-lg animate-toast-in ${s.cls}`}>
            <Icon size={16} className={`shrink-0 mt-0.5 ${s.ic}`} />
            <div className="flex-1 text-sm break-all whitespace-pre-wrap">{t.message}</div>
            <button className="shrink-0 opacity-60 hover:opacity-100"
              onClick={() => setItems((prev) => prev.filter((x) => x.id !== t.id))}>
              <X size={14} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
