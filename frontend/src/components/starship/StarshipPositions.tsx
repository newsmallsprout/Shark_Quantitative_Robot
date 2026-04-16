import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useStore } from '../../store/useStore';
import type { Position } from '../../store/useStore';
import { Crosshair } from 'lucide-react';

function initialMarginUsdt(pos: Position): number | null {
  if (pos.initialMarginUsdt != null && Number.isFinite(pos.initialMarginUsdt) && pos.initialMarginUsdt > 0) {
    return pos.initialMarginUsdt;
  }
  const lev = pos.leverage && pos.leverage > 0 ? pos.leverage : 0;
  if (pos.notionalOpenUsdt != null && Number.isFinite(pos.notionalOpenUsdt) && pos.notionalOpenUsdt > 0 && lev > 0) {
    return pos.notionalOpenUsdt / lev;
  }
  if (!pos.size || !pos.entryPrice || lev <= 0) return null;
  const cs = pos.contractSize != null && pos.contractSize > 0 ? pos.contractSize : 1;
  const n = Math.abs(pos.size) * pos.entryPrice * cs;
  return n / lev;
}

function formatMarginUsd(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return '—';
  if (v < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

const POS_PAGE = 18;

export const StarshipPositions: React.FC = () => {
  const allPositions = useStore((s) => s.positions);
  const positions = useMemo(
    () => allPositions.filter((p) => Math.abs(p.size) > 0),
    [allPositions]
  );
  const [posPage, setPosPage] = useState(0);
  const posPages = Math.max(1, Math.ceil(positions.length / POS_PAGE));
  const pageIdx = Math.min(posPage, posPages - 1);
  useEffect(() => {
    setPosPage((p) => Math.min(p, Math.max(0, posPages - 1)));
  }, [positions.length, posPages]);
  const slice = useMemo(
    () => positions.slice(pageIdx * POS_PAGE, pageIdx * POS_PAGE + POS_PAGE),
    [positions, pageIdx]
  );

  const scrollRef = useRef<HTMLDivElement>(null);
  const scrollTopRef = useRef(0);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = scrollTopRef.current;
  }, [slice]);

  return (
    <div className="ti-glass rounded-xl flex flex-col min-h-0 h-full overflow-hidden ring-1 ring-slate-200/80 shadow-sm">
      <div className="px-3 py-1.5 border-b border-slate-200/90 flex items-center gap-2 shrink-0 bg-gradient-to-r from-white to-slate-50/90">
        <Crosshair className="w-4 h-4 text-teal-600 shrink-0" />
        <span className="text-[11px] font-semibold tracking-wide text-slate-600">当前持仓</span>
        {positions.length > POS_PAGE ? (
          <span className="ml-auto flex items-center gap-1 text-[10px] text-slate-500">
            <button
              type="button"
              disabled={pageIdx <= 0}
              onClick={() => setPosPage((p) => Math.max(0, p - 1))}
              className="px-1.5 py-0.5 rounded border border-slate-200 bg-white disabled:opacity-40"
            >
              ‹
            </button>
            {pageIdx + 1}/{posPages}
            <button
              type="button"
              disabled={pageIdx >= posPages - 1}
              onClick={() => setPosPage((p) => Math.min(posPages - 1, p + 1))}
              className="px-1.5 py-0.5 rounded border border-slate-200 bg-white disabled:opacity-40"
            >
              ›
            </button>
          </span>
        ) : null}
      </div>
      <div
        ref={scrollRef}
        className="flex-1 overflow-auto min-h-0 scroll-stable"
        onScroll={() => {
          if (scrollRef.current) scrollTopRef.current = scrollRef.current.scrollTop;
        }}
      >
        <table className="w-full text-[11px] text-left min-w-[440px] leading-tight">
          <thead className="text-[9px] uppercase tracking-wider text-slate-500 border-b border-slate-200 bg-slate-50 sticky top-0 z-[1]">
            <tr>
              <th className="px-2 py-1.5 font-medium">合约</th>
              <th className="px-2 py-1.5">方向</th>
              <th className="px-2 py-1.5 text-right">杠杆</th>
              <th className="px-2 py-1.5 text-right">开仓价</th>
              <th className="px-2 py-1.5 text-right">盈亏 / 保证金</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-3 py-8 text-center text-slate-400">
                  无持仓
                </td>
              </tr>
            ) : (
              slice.map((pos) => {
                const marginU = initialMarginUsdt(pos);
                const lev = pos.leverage && pos.leverage > 0 ? Math.round(pos.leverage) : '—';
                const win = pos.unrealizedPnl >= 0;
                return (
                  <tr key={pos.symbol + pos.side} className="border-b border-slate-100/90 hover:bg-teal-50/40">
                    <td className="px-2 py-1 font-mono text-slate-900 text-[11px]">{pos.symbol}</td>
                    <td className={`px-2 py-1 font-semibold text-[11px] ${pos.side === 'long' ? 'ti-profit' : 'ti-loss'}`}>
                      {pos.side === 'long' ? '多' : '空'}
                    </td>
                    <td className="px-2 py-1 text-right font-mono text-slate-600 tabular-nums">{lev}x</td>
                    <td className="px-2 py-1 text-right font-mono text-slate-800 tabular-nums text-[11px]">
                      {pos.entryPrice.toLocaleString(undefined, { maximumFractionDigits: 6 })}
                    </td>
                    <td className={`px-2 py-1 text-right font-mono text-[11px] tabular-nums ${win ? 'ti-profit' : 'ti-loss'}`}>
                      <span className="whitespace-nowrap">
                        {win ? '+' : ''}
                        {pos.unrealizedPnl.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                        <span className="text-slate-500 font-normal">
                          {' '}
                          ({win ? '+' : ''}
                          {pos.pnlPercent.toFixed(2)}%)
                        </span>
                      </span>
                      {marginU != null && (
                        <span className="block text-[10px] text-slate-500 font-normal mt-0.5">
                          {formatMarginUsd(marginU)}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};
