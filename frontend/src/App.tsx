import { useEffect, useState } from 'react';
import { StarshipDashboard } from './components/starship/StarshipDashboard';
import TradeHistoryPage from './components/TradeHistoryPage';
import { LicenseOverlay, type LicenseStatusPayload } from './components/LicenseOverlay';
import { useStore } from './store/useStore';
import { ActivitySquare, ShieldCheck, LayoutDashboard, History } from 'lucide-react';

function App() {
  const feedMeta = useStore((s) => s.feedMeta);
  const [page, setPage] = useState<'dashboard' | 'history'>('dashboard');
  const [licensePayload, setLicensePayload] = useState<LicenseStatusPayload | null>(null);
  const [licenseFetchError, setLicenseFetchError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const res = await fetch('/api/license/status');
        if (!res.ok) {
          setLicenseFetchError(`许可证接口返回 ${res.status}`);
          setLicensePayload(null);
        } else {
          setLicensePayload((await res.json()) as LicenseStatusPayload);
          setLicenseFetchError(null);
        }
      } catch {
        setLicenseFetchError('无法连接后端（请确认 API 已启动，默认 127.0.0.1:8002）');
        setLicensePayload(null);
      }
      await useStore.getState().hydrateDashboard();
      useStore.getState().initWebSocket();
    })();
  }, []);

  return (
    <div className="min-h-screen flex flex-col text-slate-900">
      <LicenseOverlay payload={licensePayload} fetchError={licenseFetchError} />
      <header className="shrink-0 border-b border-slate-200 bg-white/95 backdrop-blur-md z-20 shadow-sm">
        {feedMeta.sandboxExecution ? (
          <div className="text-center py-1.5 text-[10px] font-mono font-bold uppercase tracking-[0.15em] text-amber-800 bg-amber-50 border-b border-amber-200">
            <ActivitySquare className="w-3 h-3 inline mr-1 align-middle" />
            沙盒执行标志开启 — 行情仍为主网
          </div>
        ) : (
          <div className="text-center py-1.5 text-[10px] font-mono font-bold uppercase tracking-[0.15em] text-slate-600 bg-slate-100 border-b border-slate-200">
            <ShieldCheck className="w-3 h-3 inline mr-1 align-middle text-teal-600" />
            主网行情 · 纸面撮合引擎
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-3 px-3 py-2 max-w-[1920px] mx-auto w-full">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg border border-teal-200 bg-teal-50 flex items-center justify-center">
              <span className="text-teal-700 font-black text-xs tracking-tighter">鲨</span>
            </div>
            <div>
              <h1 className="text-sm font-bold tracking-wide text-slate-800">战术终端</h1>
              <p className="text-[10px] text-slate-500 font-mono">Shark Quant · 本地风控</p>
            </div>
          </div>
          <nav className="flex rounded-lg border border-slate-200 overflow-hidden bg-white shadow-sm">
            <button
              type="button"
              onClick={() => setPage('dashboard')}
              className={`flex items-center gap-2 px-4 py-2 text-xs font-medium transition-colors ${
                page === 'dashboard'
                  ? 'bg-teal-50 text-teal-800 border-r border-slate-200'
                  : 'text-slate-600 hover:text-slate-900 hover:bg-slate-50'
              }`}
            >
              <LayoutDashboard className="w-4 h-4" />
              仪表盘
            </button>
            <button
              type="button"
              onClick={() => setPage('history')}
              className={`flex items-center gap-2 px-4 py-2 text-xs font-medium transition-colors ${
                page === 'history' ? 'bg-teal-50 text-teal-800' : 'text-slate-600 hover:text-slate-900 hover:bg-slate-50'
              }`}
            >
              <History className="w-4 h-4" />
              历史订单
            </button>
          </nav>
        </div>
      </header>

      {/* 仪表盘锁在视口内，避免整页滚动被 WS 刷新「打回顶部」；战报页单独可滚 */}
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
        {page === 'history' ? (
          <div className="flex-1 min-h-0 overflow-y-auto scroll-stable">
            <div className="max-w-[1400px] mx-auto p-4">
              <TradeHistoryPage />
            </div>
          </div>
        ) : (
          <StarshipDashboard />
        )}
      </div>

      <footer className="shrink-0 text-center text-[10px] text-slate-500 py-2 border-t border-slate-200 bg-white/80">
        Shark Quant · 本地风控硬限制 · 2026
      </footer>
    </div>
  );
}

export default App;
