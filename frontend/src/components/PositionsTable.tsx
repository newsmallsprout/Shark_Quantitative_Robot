import React, { useMemo, useState, useEffect } from 'react';
import { useStore } from '../store/useStore';
import { List } from 'lucide-react';
import type { Position } from '../store/useStore';

function formatPositionSize(n: number): string {
  if (!Number.isFinite(n)) return '—';
  if (n === 0) return '0';
  return String(parseFloat(n.toFixed(8)));
}

function formatPriceCell(n: number): string {
  if (!Number.isFinite(n)) return '—';
  const t = parseFloat(n.toFixed(8));
  return t.toLocaleString(undefined, { maximumFractionDigits: 8 });
}

function formatUsdCompact(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '—';
  const x = Math.abs(n);
  if (x >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (x >= 1e3) return `$${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return `$${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}

/** 名义 USDT：优先用后端算的 notional_open（张×合约面值×价），勿用 size×价（永续会错） */
function positionNotional(pos: Position): number | null {
  if (
    pos.notionalOpenUsdt != null &&
    Number.isFinite(pos.notionalOpenUsdt) &&
    pos.notionalOpenUsdt > 0
  ) {
    return pos.notionalOpenUsdt;
  }
  if (!Number.isFinite(pos.size) || !Number.isFinite(pos.entryPrice)) return null;
  if (pos.size === 0 || pos.entryPrice <= 0) return null;
  const cs = pos.contractSize != null && pos.contractSize > 0 ? pos.contractSize : 1;
  return Math.abs(pos.size) * pos.entryPrice * cs;
}

/** 初始保证金：优先 initial_margin_usdt；否则 名义/杠杆 */
function positionMargin(pos: Position): number | null {
  if (
    pos.initialMarginUsdt != null &&
    Number.isFinite(pos.initialMarginUsdt) &&
    pos.initialMarginUsdt > 0
  ) {
    return pos.initialMarginUsdt;
  }
  const lev = pos.leverage;
  const n = positionNotional(pos);
  if (n == null || lev == null || !Number.isFinite(lev) || lev <= 0) return null;
  return n / lev;
}

const POS_PAGE = 20;

const PositionsTable: React.FC = () => {
  const { positions } = useStore();
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(positions.length / POS_PAGE));
  const pageIdx = Math.min(page, pages - 1);
  useEffect(() => {
    setPage((p) => Math.min(p, Math.max(0, pages - 1)));
  }, [positions.length, pages]);
  const slice = useMemo(
    () => positions.slice(pageIdx * POS_PAGE, pageIdx * POS_PAGE + POS_PAGE),
    [positions, pageIdx]
  );

  return (
    <div className="card h-full flex flex-col">
      <div className="flex items-center justify-between mb-4 pb-4 border-b border-slate-700">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <List className="w-5 h-5 text-slate-400" />
          仓位详情
        </h2>
        <div className="flex items-center gap-2">
          {positions.length > POS_PAGE ? (
            <div className="flex items-center gap-1 text-xs text-slate-400">
              <button
                type="button"
                disabled={pageIdx <= 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                className="px-2 py-0.5 rounded bg-slate-800 disabled:opacity-40"
              >
                上一页
              </button>
              <span className="font-mono">
                {pageIdx + 1}/{pages}
              </span>
              <button
                type="button"
                disabled={pageIdx >= pages - 1}
                onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
                className="px-2 py-0.5 rounded bg-slate-800 disabled:opacity-40"
              >
                下一页
              </button>
            </div>
          ) : null}
          <span className="bg-slate-800 text-slate-300 px-2 py-1 rounded text-xs font-mono">
            {positions.length} 笔
          </span>
        </div>
      </div>

      <div className="overflow-x-auto flex-1 min-h-0">
        <table className="w-full text-sm text-left min-w-[720px]">
          <thead className="text-xs text-slate-400 uppercase bg-slate-800/50">
            <tr>
              <th className="px-3 py-3 font-medium rounded-tl-lg whitespace-nowrap">合约</th>
              <th className="px-3 py-3 font-medium whitespace-nowrap">方向</th>
              <th className="px-3 py-3 font-medium text-right whitespace-nowrap">数量</th>
              <th className="px-3 py-3 font-medium text-right whitespace-nowrap">开仓价</th>
              <th className="px-3 py-3 font-medium text-right whitespace-nowrap">名义(USDT)</th>
              <th className="px-3 py-3 font-medium text-right whitespace-nowrap">杠杆</th>
              <th className="px-3 py-3 font-medium text-right whitespace-nowrap">保证金</th>
              <th className="px-3 py-3 font-medium text-right rounded-tr-lg whitespace-nowrap">未实现盈亏 / 收益率</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={8} className="text-center py-8 text-slate-500">
                  暂无持仓
                </td>
              </tr>
            ) : (
              slice.map((pos, idx) => {
                const notional = positionNotional(pos);
                const margin = positionMargin(pos);
                const lev =
                  pos.leverage != null && Number.isFinite(pos.leverage) && pos.leverage > 0
                    ? Math.round(pos.leverage)
                    : null;
                const mm =
                  (pos.margin_mode || '').toLowerCase() === 'isolated'
                    ? '逐仓'
                    : (pos.margin_mode || '').toLowerCase() === 'cross'
                      ? '全仓'
                      : pos.margin_mode || '';

                const pctOnNom =
                  pos.pnlPercentNotional != null && Number.isFinite(pos.pnlPercentNotional)
                    ? pos.pnlPercentNotional
                    : notional != null && notional > 0
                      ? (pos.unrealizedPnl / notional) * 100
                      : null;

                return (
                  <tr key={`${pos.symbol}-${idx}`} className="border-b border-slate-800/50 hover:bg-slate-800/30 transition-colors">
                    <td className="px-3 py-3 font-bold text-slate-200 whitespace-nowrap">{pos.symbol}</td>
                    <td className="px-3 py-3 whitespace-nowrap">
                      <span
                        className={`px-2 py-1 rounded text-xs font-bold ${
                          pos.side === 'long' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-red-500/20 text-red-400'
                        }`}
                      >
                        {pos.side === 'long' ? '多' : '空'}
                      </span>
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-xs whitespace-nowrap">
                      {formatPositionSize(pos.size)}
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-xs whitespace-nowrap">
                      ${formatPriceCell(pos.entryPrice)}
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-xs text-slate-300 whitespace-nowrap">
                      {notional != null ? formatUsdCompact(notional) : '—'}
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-xs whitespace-nowrap">
                      {lev != null ? (
                        <span title={mm ? `保证金模式: ${mm}` : undefined}>
                          {lev}x
                          {mm ? <span className="text-slate-500 text-[10px] ml-1">{mm}</span> : null}
                        </span>
                      ) : (
                        <span className="text-slate-500">—</span>
                      )}
                    </td>
                    <td className="px-3 py-3 text-right font-mono text-xs text-amber-200/90 whitespace-nowrap">
                      {margin != null ? formatUsdCompact(margin) : '—'}
                    </td>
                    <td className="px-3 py-3 text-right font-mono whitespace-nowrap align-top">
                      <div className={pos.unrealizedPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                        {pos.unrealizedPnl >= 0 ? '+' : ''}
                        {pos.unrealizedPnl.toFixed(2)}
                      </div>
                      <div className="text-[10px] text-slate-500 mt-0.5 leading-tight space-y-0.5">
                        <div>
                          保{' '}
                          <span className={pos.pnlPercent >= 0 ? 'text-emerald-400/90' : 'text-red-400/90'}>
                            {pos.pnlPercent >= 0 ? '+' : ''}
                            {pos.pnlPercent.toFixed(2)}%
                          </span>
                          <span className="text-slate-600">（保证金）</span>
                        </div>
                        {pctOnNom != null && Number.isFinite(pctOnNom) ? (
                          <div>
                            名{' '}
                            <span className={pctOnNom >= 0 ? 'text-emerald-400/90' : 'text-red-400/90'}>
                              {pctOnNom >= 0 ? '+' : ''}
                              {pctOnNom.toFixed(4)}%
                            </span>
                            <span className="text-slate-600">（名义）</span>
                          </div>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <p className="text-[10px] text-slate-500 mt-3 pt-3 border-t border-slate-800 leading-relaxed">
        <span className="text-slate-400 font-medium">口径说明：</span>
        「名义」= 数量×开仓价；「保证金」= 名义÷杠杆。美元盈亏由标记价相对开仓价计算；收益率「保」以保证金为分母（ROE），「名」以名义为分母，可与名义列交叉核对。
      </p>
      <p className="text-[10px] text-slate-500 mt-2 leading-relaxed">
        <span className="text-slate-400 font-medium">Darwin（AI 策略学习）：</span>
        平仓后系统会把战报写入配置的战报目录；在顶部导航打开「历史仓位」可查看。若要在{' '}
        <code className="text-slate-400">config/settings.yaml</code> 里开启 LLM 自动改参，将{' '}
        <code className="text-slate-400">darwin.apply_llm_patches</code> 设为 <code className="text-slate-400">true</code>，并配置{' '}
        <code className="text-slate-400">darwin.llm_provider</code>。逻辑代码在仓库{' '}
        <code className="text-slate-400">src/darwin/</code>。
      </p>
    </div>
  );
};

export default PositionsTable;
