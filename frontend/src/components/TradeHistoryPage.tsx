import React, { useCallback, useEffect, useState } from 'react';
import { History, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';

export interface TradeHistoryRow {
  file: string;
  closed_at: number;
  symbol: string;
  side: string;
  entry_price: number;
  exit_price: number;
  closed_size: number;
  contract_size?: number;
  base_qty?: number;
  entry_notional_usdt?: number;
  exit_notional_usdt?: number;
  leverage: number;
  margin_mode: string;
  gross_pnl: number;
  fees: number;
  net_pnl: number;
  exit_reason: string;
  trading_mode: string | null;
  duration_sec: number;
  max_favorable_unrealized: number;
  max_adverse_unrealized: number;
  entry_snapshot: Record<string, unknown>;
}

function formatTs(t: number): string {
  if (!t || !Number.isFinite(t)) return '—';
  const d = new Date(t * 1000);
  return d.toLocaleString('zh-CN', { hour12: false });
}

function formatNum(n: number, digits = 4): string {
  if (!Number.isFinite(n)) return '—';
  const x = Math.abs(n);
  if (x >= 1000) return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (x >= 1) return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: digits });
  return n.toLocaleString('zh-CN', { minimumFractionDigits: 4, maximumFractionDigits: 8 });
}

function exitReasonZh(reason: string): string {
  const m: Record<string, string> = {
    opposite_fill: '对手盘平仓',
    reduce_only: '仅减仓',
    reverse_open_opposite: '反手开仓',
    kill_switch_twap: '紧急平仓(旧TWAP)',
    kill_switch_flat: '紧急平仓(市价)',
    exit_atr_initial: 'ATR 初始止损',
    exit_chandelier_trail: 'Chandelier 追踪止盈',
    exit_obi_preempt: 'OBI 抢先平仓',
    exit_alpha_decay_time: '时间止损(Alpha 衰减)',
    core_bracket_tp: 'Core 限价止盈',
    core_bracket_sl: 'Core 限价止损',
    core_bracket_stop: 'Core 止损(市价,旧逻辑)',
  };
  return m[reason] || reason || '—';
}

const TradeHistoryPage: React.FC = () => {
  const [rows, setRows] = useState<TradeHistoryRow[]>([]);
  const [sourceDir, setSourceDir] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/trade_history?limit=500');
      const text = await res.text();
      let data: { items?: TradeHistoryRow[]; source_dir?: string; detail?: string } = {};
      try {
        data = text ? JSON.parse(text) : {};
      } catch {
        throw new Error(text || `HTTP ${res.status}`);
      }
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      setRows((data.items as TradeHistoryRow[]) || []);
      setSourceDir(String(data.source_dir || ''));
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="card flex flex-col min-h-[480px] border-slate-200">
      <div className="flex flex-wrap items-center justify-between gap-4 mb-4 pb-4 border-b border-slate-200">
        <h2 className="text-lg font-semibold flex items-center gap-2 text-slate-800">
          <History className="w-5 h-5 text-teal-600" />
          历史仓位
        </h2>
        <div className="flex items-center gap-3">
          {sourceDir && (
            <span className="text-[10px] text-slate-500 font-mono max-w-[280px] truncate" title={sourceDir}>
              数据源: {sourceDir}
            </span>
          )}
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="btn flex items-center gap-2 bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 px-3 py-1.5 rounded text-sm disabled:opacity-50 shadow-sm"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </button>
          <span className="bg-slate-100 text-slate-700 px-2 py-1 rounded text-xs font-mono border border-slate-200">
            {rows.length} 条
          </span>
        </div>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded border border-red-200 bg-red-50 text-red-800 text-sm">{error}</div>
      )}

      <div className="overflow-x-auto flex-1 -mx-1 scroll-stable">
        <table className="w-full text-sm text-left min-w-[960px]">
          <thead className="text-xs text-slate-500 uppercase bg-slate-50 border-b border-slate-200">
            <tr>
              <th className="px-2 py-3 w-8" />
              <th className="px-3 py-3 font-medium">平仓时间</th>
              <th className="px-3 py-3 font-medium">合约</th>
              <th className="px-3 py-3 font-medium">方向</th>
              <th className="px-3 py-3 font-medium text-right">名义</th>
              <th className="px-3 py-3 font-medium text-right">入场价</th>
              <th className="px-3 py-3 font-medium text-right">平仓价</th>
              <th className="px-3 py-3 font-medium text-right">杠杆</th>
              <th className="px-3 py-3 font-medium text-right">净盈亏</th>
              <th className="px-3 py-3 font-medium">平仓原因</th>
              <th className="px-3 py-3 font-medium rounded-tr-lg">模式</th>
            </tr>
          </thead>
          <tbody>
            {loading && rows.length === 0 ? (
              <tr>
                <td colSpan={11} className="text-center py-16 text-slate-500">
                  加载中…
                </td>
              </tr>
            ) : rows.length === 0 ? (
              <tr>
                <td colSpan={11} className="text-center py-16 text-slate-500">
                  暂无历史记录。纸面撮合平仓后会写入 Darwin 战报目录；请确认已开启{' '}
                  <code className="text-slate-600 bg-slate-100 px-1 rounded">darwin.log_autopsies</code> 且目录可写。
                </td>
              </tr>
            ) : (
              rows.map((r) => {
                const open = expanded === r.file;
                return (
                  <React.Fragment key={r.file}>
                    <tr className="border-b border-slate-100 hover:bg-slate-50/80 transition-colors">
                      <td className="px-2 py-2">
                        <button
                          type="button"
                          onClick={() => setExpanded(open ? null : r.file)}
                          className="p-1 text-slate-500 hover:text-slate-800"
                          aria-label="展开详情"
                        >
                          {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                        </button>
                      </td>
                      <td className="px-3 py-2 font-mono text-xs text-slate-600 whitespace-nowrap">
                        {formatTs(r.closed_at)}
                      </td>
                      <td className="px-3 py-2 font-bold text-slate-800">{r.symbol}</td>
                      <td className="px-3 py-2">
                        <span
                          className={`px-2 py-0.5 rounded text-xs font-bold ${
                            r.side === 'long' ? 'bg-emerald-100 text-emerald-800' : 'bg-red-100 text-red-700'
                          }`}
                        >
                          {r.side === 'long' ? '多' : '空'}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right font-mono text-xs">${formatNum(Number(r.entry_notional_usdt ?? 0), 2)}</td>
                      <td className="px-3 py-2 text-right font-mono text-xs">${formatNum(r.entry_price)}</td>
                      <td className="px-3 py-2 text-right font-mono text-xs">${formatNum(r.exit_price)}</td>
                      <td className="px-3 py-2 text-right font-mono text-xs">
                        {Number.isFinite(r.leverage) ? `${Math.round(r.leverage)}x` : '—'}
                      </td>
                      <td className="px-3 py-2 text-right font-mono">
                        <span className={r.net_pnl >= 0 ? 'text-emerald-700' : 'text-red-600'}>
                          {r.net_pnl >= 0 ? '+' : ''}
                          {formatNum(r.net_pnl, 2)}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-xs text-slate-600">{exitReasonZh(r.exit_reason)}</td>
                      <td className="px-3 py-2 text-xs text-slate-500 font-mono">{r.trading_mode || '—'}</td>
                    </tr>
                    {open && (
                      <tr className="bg-slate-50 border-b border-slate-100">
                        <td colSpan={11} className="px-4 py-3">
                          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
                            <div>
                              <p className="text-slate-500 uppercase tracking-wider mb-2">路径统计</p>
                              <ul className="space-y-1 text-slate-700 font-mono">
                                <li>
                                  最大有利浮动:{' '}
                                  <span className="text-emerald-700">{formatNum(r.max_favorable_unrealized, 2)}</span>
                                </li>
                                <li>
                                  最大不利浮动:{' '}
                                  <span className="text-red-600">{formatNum(r.max_adverse_unrealized, 2)}</span>
                                </li>
                                <li>持仓时长: {r.duration_sec.toFixed(1)} s</li>
                                <li>张数: {formatNum(r.closed_size, 8)}</li>
                                <li>合约面值: {formatNum(Number(r.contract_size ?? 0), 8)}</li>
                                <li>标的数量: {formatNum(Number(r.base_qty ?? 0), 8)}</li>
                                <li>
                                  毛利: <span className="text-slate-800">{formatNum(r.gross_pnl, 4)}</span>
                                </li>
                                <li className="rounded border border-red-200 bg-red-50 px-2 py-1.5 -mx-0.5">
                                  <span className="text-red-800">摩擦损耗（手续费合计）</span>{' '}
                                  <span className="text-red-700 font-semibold">{formatNum(r.fees, 4)} USDT</span>
                                  <span className="block text-[10px] text-red-600/90 font-normal mt-1">
                                    名义 × 费率；与杠杆无关。净利 = 毛利 − 该项。
                                  </span>
                                </li>
                                <li>
                                  净盈亏:{' '}
                                  <span className={r.net_pnl >= 0 ? 'text-emerald-700' : 'text-red-600'}>
                                    {formatNum(r.net_pnl, 4)}
                                  </span>
                                </li>
                                <li>保证金模式: {r.margin_mode || '—'}</li>
                              </ul>
                            </div>
                            <div>
                              <p className="text-slate-500 uppercase tracking-wider mb-2">开仓快照 (entry_snapshot)</p>
                              <pre className="p-3 rounded bg-white border border-slate-200 overflow-x-auto text-slate-700 max-h-48 shadow-inner">
                                {JSON.stringify(r.entry_snapshot || {}, null, 2)}
                              </pre>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default TradeHistoryPage;
