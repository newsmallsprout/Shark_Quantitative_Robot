import React, { useState, useEffect, useRef } from 'react';
import { useStore } from '../../store/useStore';
import type { SystemMode } from '../../store/useStore';
import {
  Gauge,
  AlertOctagon,
  ShieldCheck,
  Zap,
  Flame,
  ActivitySquare,
  AlertTriangle,
} from 'lucide-react';

function fmtFunding(fr: number): string {
  return `${(fr * 100).toFixed(4)}%`;
}

function fmtHms(sec: number): string {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h${m}m`;
}

export const WeaponSpecsDeck: React.FC = () => {
  const {
    activeSymbol,
    contractSpecs,
    resonanceMetrics,
    mode,
    setMode,
    isRunning,
    toggleEngine,
    killSwitch,
  } = useStore();
  const [showKill, setShowKill] = useState(false);
  const prevSpread = useRef<number | null>(null);
  const [spreadFlash, setSpreadFlash] = useState(false);

  const spec = contractSpecs[activeSymbol];
  const spread = spec?.spread ?? 0;
  const obi = resonanceMetrics.obi;
  const atrPct = (resonanceMetrics.atr_pct ?? 0) * 100;
  const ks = resonanceMetrics.kill_switch_progress;

  useEffect(() => {
    if (prevSpread.current != null && Math.abs(spread - prevSpread.current) > 1e-8) {
      setSpreadFlash(true);
      const t = window.setTimeout(() => setSpreadFlash(false), 350);
      prevSpread.current = spread;
      return () => window.clearTimeout(t);
    }
    prevSpread.current = spread;
    return undefined;
  }, [spread]);

  const obiPct = Math.max(0, Math.min(100, ((obi + 1) / 2) * 100));
  const funding = spec?.funding_rate ?? 0;
  const nextFt = spec?.next_funding_time;
  const fundEtaSec =
    typeof nextFt === 'number' && nextFt > 1e12
      ? Math.max(0, Math.floor(nextFt / 1000 - Date.now() / 1000))
      : typeof nextFt === 'number' && nextFt > 0
        ? Math.max(0, Math.floor(nextFt - Date.now() / 1000))
        : null;

  const handleMode = async (newMode: SystemMode) => {
    if (newMode === mode || newMode === 'HALTED') return;
    try {
      const res = await fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'SET_TRADING_MODE', mode: newMode }),
      });
      if (res.ok) setMode(newMode);
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="ti-glass-strong rounded-lg flex flex-col h-full min-h-0 overflow-hidden">
      <div className="px-3 py-2 border-b border-white/[0.08] flex items-center gap-2">
        <Gauge className="w-4 h-4 text-[#FFB300]" />
        <span className="text-[11px] font-semibold tracking-[0.2em] text-[#7a8499] uppercase">
          武器参数 · 合约情报
        </span>
      </div>

      <div className="p-3 space-y-4 flex-1 overflow-y-auto min-h-0">
        <div>
          <p className="text-[9px] text-[#5c6578] uppercase tracking-widest mb-1">微观防线</p>
          <div className="space-y-2 font-mono text-[11px]">
            <div className="flex justify-between gap-2">
              <span className="text-[#7a8499]">资金费率</span>
              <span className={funding >= 0 ? 'ti-profit' : 'ti-loss'}>{fmtFunding(funding)}</span>
            </div>
            <div className="flex justify-between gap-2 text-[#6a7388]">
              <span>距结算</span>
              <span>{fundEtaSec != null ? fmtHms(fundEtaSec) : '—'}</span>
            </div>
            <div className="flex justify-between gap-2">
              <span className="text-[#7a8499]">买卖微差</span>
              <span className={`text-[#c5cad8] transition-colors ${spreadFlash ? 'ti-warn' : ''}`}>
                {spread > 0 ? spread.toPrecision(5) : '—'}
              </span>
            </div>
            <div className="flex justify-between gap-2">
              <span className="text-[#7a8499]">标记价格</span>
              <span className="text-[#c5cad8]">{spec?.mark_price ? spec.mark_price.toFixed(6) : '—'}</span>
            </div>
            <div className="flex justify-between gap-2">
              <span className="text-[#7a8499]">指数价格</span>
              <span className="text-[#c5cad8]">{spec?.index_price ? spec.index_price.toFixed(6) : '—'}</span>
            </div>
            <div className="flex justify-between gap-2">
              <span className="text-[#7a8499]">24h 成交量</span>
              <span className="text-[#c5cad8]">
                {typeof spec?.volume_24h === 'number'
                  ? spec.volume_24h.toLocaleString(undefined, { maximumFractionDigits: 0 })
                  : '—'}
              </span>
            </div>
            <div>
              <div className="flex justify-between text-[#7a8499] mb-1">
                <span>多空失衡 OBI</span>
                <span className="text-[#c5cad8]">{obi.toFixed(3)}</span>
              </div>
              <div className="h-2 rounded-full bg-[#1a1220] overflow-hidden border border-white/[0.06] relative">
                <div
                  className="absolute inset-y-0 w-1 bg-white/30"
                  style={{ left: `${50}%`, transform: 'translateX(-50%)' }}
                />
                <div
                  className="h-full rounded-full transition-all duration-300"
                  style={{
                    width: `${obiPct}%`,
                    background:
                      obiPct >= 50
                        ? 'linear-gradient(90deg, #3d1a12 0%, #2dd4bf 100%)'
                        : 'linear-gradient(90deg, #FF0055 0%, #3d0a18 100%)',
                  }}
                />
              </div>
              <div className="flex justify-between text-[9px] text-[#5c6578] mt-0.5">
                <span>卖盘</span>
                <span>买盘</span>
              </div>
            </div>
            <div className="flex justify-between gap-2">
              <span className="text-[#7a8499]">1m 波动率 (ATR)</span>
              <span className="text-[#2dd4bf]">{atrPct.toFixed(3)}%</span>
            </div>
          </div>
        </div>

        <div>
          <p className="text-[9px] text-[#5c6578] uppercase tracking-widest mb-2">战术姿态</p>
          <div className="grid grid-cols-3 gap-1.5">
            <button
              type="button"
              disabled={!isRunning}
              onClick={() => handleMode('NEUTRAL')}
              className={`py-2 px-1 rounded border text-[10px] font-semibold flex flex-col items-center gap-1 transition-all ${
                mode === 'NEUTRAL'
                  ? 'border-[#2dd4bf]/45 bg-[#2dd4bf]/10 text-[#2dd4bf]'
                  : 'border-white/[0.08] text-[#7a8499] hover:border-white/20'
              }`}
            >
              <ShieldCheck className="w-4 h-4" />
              观望
            </button>
            <button
              type="button"
              disabled={!isRunning}
              onClick={() => handleMode('ATTACK')}
              className={`py-2 px-1 rounded border text-[10px] font-semibold flex flex-col items-center gap-1 transition-all ${
                mode === 'ATTACK'
                  ? 'border-[#FFB300]/55 bg-[#FFB300]/12 text-[#FFB300]'
                  : 'border-white/[0.08] text-[#7a8499] hover:border-white/20'
              }`}
            >
              <Zap className="w-4 h-4" />
              潜伏
            </button>
            <button
              type="button"
              disabled={!isRunning}
              onClick={() => handleMode('BERSERKER')}
              className={`py-2 px-1 rounded border text-[10px] font-semibold flex flex-col items-center gap-1 transition-all ${
                mode === 'BERSERKER'
                  ? 'border-[#FF0055]/55 bg-[#FF0055]/12 text-[#FF0055] shadow-[0_0_12px_rgba(255,0,85,0.25)]'
                  : 'border-white/[0.08] text-[#7a8499] hover:border-white/20'
              }`}
            >
              <Flame className="w-4 h-4" />
              狂暴
            </button>
          </div>
          <p className="text-[9px] text-[#5c6578] mt-1.5 leading-snug">
            观望＝中性防御 · 潜伏＝进攻接敌 · 狂暴＝极限杠杆
          </p>
        </div>

        <div className="rounded border border-white/[0.06] p-2 grid grid-cols-2 gap-2 text-[10px]">
          <div className="flex items-center gap-1 text-[#7a8499]">
            <ActivitySquare className="w-3.5 h-3.5 text-[#e7b15a]" />
            WS 延迟
          </div>
          <div className="font-mono text-right text-[#c5cad8]">{resonanceMetrics.ws_latency || '<1'} ms</div>
          <div className="flex items-center gap-1 text-[#7a8499]">
            <AlertTriangle className="w-3.5 h-3.5 text-[#FFB300]" />
            重连
          </div>
          <div className="font-mono text-right text-[#c5cad8]">{resonanceMetrics.ws_reconnects}</div>
        </div>

        {ks?.active && (
          <div className="rounded border border-[#FF0055]/40 bg-[#FF0055]/[0.06] p-2 text-[10px] text-[#FF0055]">
            <div className="flex justify-between font-mono mb-1">
              <span>清仓执行</span>
              <span>
                {ks.executed_chunks}/{ks.total_chunks}
              </span>
            </div>
            <div className="h-1.5 bg-[#1a1816]/70 rounded overflow-hidden">
              <div
                className="h-full bg-[#FF0055] transition-all"
                style={{ width: `${(ks.executed_chunks / ks.total_chunks) * 100}%` }}
              />
            </div>
          </div>
        )}

        <button
          type="button"
          onClick={toggleEngine}
          className={`w-full py-2.5 rounded border text-[11px] font-bold tracking-wide ${
            isRunning
              ? 'border-white/[0.1] bg-white/[0.04] text-[#8b93a8]'
              : 'border-[#2dd4bf]/35 bg-[#2dd4bf]/10 text-[#2dd4bf]'
          }`}
        >
          {isRunning ? '暂停引擎' : '启动引擎'}
        </button>

        {showKill ? (
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setShowKill(false)}
              className="flex-1 py-2 rounded border border-white/15 text-[11px] text-[#8b93a8]"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => {
                killSwitch();
                setShowKill(false);
              }}
              className="flex-1 py-2 rounded border border-[#FF0055] bg-[#FF0055]/20 text-[11px] font-bold text-[#FF0055]"
            >
              确认核爆
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setShowKill(true)}
            disabled={!isRunning && mode === 'HALTED'}
            className="w-full py-3 rounded border-2 border-dashed border-[#FF0055]/50 bg-[repeating-linear-gradient(45deg,transparent,transparent_4px,rgba(255,0,85,0.07)_4px,rgba(255,0,85,0.07)_8px)] text-[#FF0055] text-[11px] font-bold uppercase tracking-[0.15em] flex items-center justify-center gap-2 disabled:opacity-40"
          >
            <AlertOctagon className="w-5 h-5" />
            一键核爆 · 清仓全部
          </button>
        )}
      </div>
    </div>
  );
};
