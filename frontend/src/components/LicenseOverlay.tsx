import { KeyRound, ShieldAlert } from 'lucide-react';

export type LicenseStatusPayload = {
  license_valid: boolean;
  license_locked: boolean;
  skip_license_check: boolean;
  public_key_present: boolean;
  license_file_present: boolean;
  license_path: string;
  message: string;
  hint_zh: string;
};

type Props = {
  payload: LicenseStatusPayload | null;
  fetchError: string | null;
};

/**
 * Full-screen gate when backend reports an invalid / missing commercial license.
 * Dev bypass: SKIP_LICENSE_CHECK=1 on the Python process hides this overlay.
 */
export function LicenseOverlay({ payload, fetchError }: Props) {
  if (payload?.skip_license_check) {
    return null;
  }
  if (payload?.license_valid) {
    return null;
  }

  const backendUnreachable = !payload && Boolean(fetchError);
  const detail = payload?.message ?? fetchError ?? '无法获取许可证状态';
  const hint = payload?.hint_zh ?? '请联系创作者获取授权与 license.key。';

  return (
    <div
      className="fixed inset-0 z-[100] flex flex-col items-center justify-center gap-6 px-6 py-10 bg-slate-950/92 backdrop-blur-md text-slate-100"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="license-gate-title"
      aria-describedby="license-gate-desc"
    >
      <div className="max-w-lg w-full rounded-2xl border border-amber-500/40 bg-slate-900/90 shadow-2xl shadow-amber-900/20 p-8 text-center">
        <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full border border-amber-500/50 bg-amber-500/10">
          <ShieldAlert className="h-7 w-7 text-amber-400" aria-hidden />
        </div>
        <h2 id="license-gate-title" className="text-lg font-semibold tracking-tight text-white">
          {backendUnreachable ? '无法连接后端服务' : '需要创作者签发的许可证'}
        </h2>
        <p id="license-gate-desc" className="mt-3 text-sm leading-relaxed text-slate-300">
          {backendUnreachable ? (
            <>
              前端已启动，但未能在 <span className="font-mono text-slate-400">/api/license/status</span> 取得响应。请先启动 Python 主进程（默认 API
              <span className="font-mono text-slate-400"> 127.0.0.1:8002</span>
              ），并确认 Vite 代理配置正确。
            </>
          ) : (
            <>
              您当前从源码运行本终端，但未检测到有效的商业授权。为保障策略与执行逻辑的分发权益，请向创作者申请
              <span className="text-teal-300 font-mono text-xs"> license/license.key </span>
              与配套公钥校验；获得授权后即可正常使用仪表盘与 API。
            </>
          )}
        </p>
        <div className="mt-5 rounded-lg border border-slate-700 bg-slate-950/80 px-4 py-3 text-left text-xs font-mono text-slate-400 leading-relaxed">
          <div className="flex items-start gap-2 text-amber-200/90">
            <KeyRound className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <span>{detail}</span>
          </div>
          {!backendUnreachable && <p className="mt-3 text-slate-500">{hint}</p>}
        </div>
        {!backendUnreachable && (
          <p className="mt-6 text-[11px] text-slate-500">
            本地开发可设置环境变量 <span className="font-mono text-slate-400">SKIP_LICENSE_CHECK=1</span>（请勿用于生产）。
          </p>
        )}
      </div>
    </div>
  );
}
