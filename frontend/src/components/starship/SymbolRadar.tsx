import React, { useMemo } from 'react';
import { useStore } from '../../store/useStore';
import { MicroSparkline } from './MicroSparkline';
import { Radar } from 'lucide-react';

function formatPx(n: number | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (n >= 1) return n.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return n.toPrecision(4);
}

function formatChgPct(n: number | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  const s = n >= 0 ? '+' : '';
  return `${s}${n.toFixed(2)}%`;
}

export const SymbolRadar: React.FC = () => {
  const { availableSymbols, contractSpecs, activeSymbol, setActiveSymbol, symbolPriceTrail, positions } =
    useStore();

  const ranked = useMemo(() => {
    return [...availableSymbols].map((sym) => {
      const trail = symbolPriceTrail[sym] ?? [];
      let vol = 0;
      if (trail.length > 2) {
        for (let i = 1; i < trail.length; i++) {
          vol += Math.abs(trail[i]! - trail[i - 1]!);
        }
        vol /= trail.length - 1;
      }
      const rel = vol / (trail[0] || 1e-8);
      return { sym, vol: rel, trail };
    }).sort((a, b) => b.vol - a.vol);
  }, [availableSymbols, symbolPriceTrail]);

  return (
    <div className="ti-glass rounded-lg flex flex-col min-h-0 h-full overflow-hidden">
      <div className="px-3 py-2 border-b border-white/[0.06] flex items-center gap-2 shrink-0">
        <Radar className="w-4 h-4 text-[#e7b15a]" />
        <span className="text-[11px] font-semibold tracking-[0.2em] text-[#8a8580] uppercase">
          标的雷达 · 狙击名单
        </span>
      </div>
      <div className="flex-1 overflow-y-auto min-h-0 ti-matrix-scroll">
        {ranked.map(({ sym, vol, trail }) => {
          const spec = contractSpecs[sym];
          const last = spec?.last_price;
          const chg = spec?.change_24h_pct;
          const active = sym === activeSymbol;
          const glow = Math.min(1, vol * 400);
          const pos = positions.find((p) => p.symbol === sym && p.size > 0);
          const bep = pos?.breakEvenPrice;
          return (
            <button
              key={sym}
              type="button"
              onClick={() => setActiveSymbol(sym)}
              className={`w-full text-left px-3 py-2 flex items-center gap-2 border-b border-white/[0.04] transition-colors ${
                active ? 'bg-[#2dd4bf]/[0.1]' : 'hover:bg-white/[0.03]'
              }`}
            >
              <MicroSparkline
                values={trail.length > 1 ? trail : last ? [last * 0.999, last * 1.001] : []}
                accentClass={active ? 'stroke-[#2dd4bf]' : 'stroke-[#8a8580]'}
                breakEvenPrice={bep != null && bep > 0 ? bep : undefined}
              />
              <div className="flex-1 min-w-0">
                <div
                  className="text-xs font-mono font-medium truncate"
                  style={{
                    color: `rgba(231, 177, 90, ${0.55 + glow * 0.45})`,
                  }}
                >
                  {sym.replace('/USDT', '')}
                </div>
                <div className="flex items-center justify-between gap-1">
                  <span className="text-[10px] text-[#7a8499] font-mono">{formatPx(last)}</span>
                  <span
                    className={`text-[10px] font-mono ${
                      chg == null || !Number.isFinite(chg)
                        ? 'text-[#7a8499]'
                        : chg >= 0
                          ? 'text-emerald-400'
                          : 'text-rose-400'
                    }`}
                  >
                    {formatChgPct(chg)}
                  </span>
                </div>
              </div>
              <div className="text-[9px] text-[#7a8499] w-8 text-right font-mono">
                {trail.length > 2 ? `${(vol * 100).toFixed(1)}‰` : '—'}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
};
