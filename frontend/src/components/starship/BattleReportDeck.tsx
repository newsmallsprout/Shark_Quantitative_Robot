import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { ScrollText, RefreshCw } from 'lucide-react';

type TradeHistoryRow = {
  file: string;
  closed_at: number;
  symbol: string;
  side: string;
  net_pnl: number;
  exit_reason: string;
};

type LeaderboardRow = {
  file?: string;
  closed_at?: number;
  symbol?: string;
  side?: string;
  net_pnl?: number;
  gross_pnl?: number;
  fees?: number;
  exit_reason?: string;
  duration_sec?: number;
  leverage?: number;
};

type LogsResponse = { logs?: string[] };
type SceneLeaderboardItem = {
  scene_key?: string;
  priority_score?: number;
  wr?: number;
  net?: number;
  n?: number;
  regime?: string;
  symbol?: string;
  side?: string;
  strategy?: string;
  quadrant?: string;
  ai_bucket?: string;
};
type TradeHistoryResponse = {
  items?: TradeHistoryRow[];
  source_dir?: string;
  detail?: string;
  summary?: {
    total_count?: number;
    wins?: number;
    losses?: number;
    win_rate?: number;
    net_total?: number;
    fees_total?: number;
    worst_losses?: LeaderboardRow[];
    best_wins?: LeaderboardRow[];
    worst_losses_top_sum?: number;
    best_wins_top_sum?: number;
    strategy_hints?: string[];
  };
};
type ResonanceResponse = {
  adaptation?: {
    scene_leaderboard?: SceneLeaderboardItem[];
  };
};

function fmtNum(n: number, digits = 2): string {
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function exitReasonZh(reason: string): string {
  const m: Record<string, string> = {
    opposite_fill: '对手盘平仓',
    reduce_only: '仅减仓',
    reverse_open_opposite: '反手开仓',
    kill_switch_flat: '紧急平仓',
    exit_atr_initial: 'ATR 初始止损',
    exit_chandelier_trail: '追踪止盈',
    exit_obi_preempt: 'OBI 抢先平仓',
    exit_alpha_decay_time: '时间止损',
    core_bracket_tp: '限价止盈',
    core_bracket_sl: '限价止损',
    isolated_margin_ruin: '逐仓保证金击穿',
    eod_flat: '回测/会话结束强平',
    liquidated: '强平',
  };
  return m[reason] || reason || '—';
}

export const BattleReportDeck: React.FC = () => {
  const [rows, setRows] = useState<TradeHistoryRow[]>([]);
  const [logs, setLogs] = useState<string[]>([]);
  const [sceneTop, setSceneTop] = useState<SceneLeaderboardItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summaryState, setSummaryState] = useState({
    total: 0,
    wins: 0,
    losses: 0,
    winRate: 0,
    net: 0,
    feesTotal: 0,
    worstLosses: [] as LeaderboardRow[],
    bestWins: [] as LeaderboardRow[],
    worstTopSum: 0,
    bestTopSum: 0,
    strategyHints: [] as string[],
  });

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [historyRes, logsRes, resoRes] = await Promise.all([
        fetch('/api/trade_history?limit=24&offset=0'),
        fetch('/api/logs?limit=12&offset=0'),
        fetch('/api/resonance_metrics'),
      ]);

      const historyText = await historyRes.text();
      const logsText = await logsRes.text();
      const resoText = await resoRes.text();

      let history: TradeHistoryResponse = {};
      let logsJson: LogsResponse = {};
      let resoJson: ResonanceResponse = {};
      try {
        history = historyText ? JSON.parse(historyText) : {};
      } catch {
        throw new Error(historyText || `trade_history HTTP ${historyRes.status}`);
      }
      try {
        logsJson = logsText ? JSON.parse(logsText) : {};
      } catch {
        throw new Error(logsText || `logs HTTP ${logsRes.status}`);
      }
      try {
        resoJson = resoText ? JSON.parse(resoText) : {};
      } catch {
        resoJson = {};
      }

      if (!historyRes.ok) throw new Error(history.detail || `trade_history HTTP ${historyRes.status}`);
      if (!logsRes.ok) throw new Error(`logs HTTP ${logsRes.status}`);

      setRows(Array.isArray(history.items) ? history.items : []);
      setLogs(Array.isArray(logsJson.logs) ? logsJson.logs : []);
      setSceneTop(Array.isArray(resoJson.adaptation?.scene_leaderboard) ? resoJson.adaptation.scene_leaderboard.slice(0, 3) : []);
      setSummaryState({
        total: Number(history.summary?.total_count ?? 0),
        wins: Number(history.summary?.wins ?? 0),
        losses: Number(history.summary?.losses ?? 0),
        winRate: Number(history.summary?.win_rate ?? 0) * 100,
        net: Number(history.summary?.net_total ?? 0),
        feesTotal: Number(history.summary?.fees_total ?? 0),
        worstLosses: Array.isArray(history.summary?.worst_losses) ? history.summary!.worst_losses! : [],
        bestWins: Array.isArray(history.summary?.best_wins) ? history.summary!.best_wins! : [],
        worstTopSum: Number(history.summary?.worst_losses_top_sum ?? 0),
        bestTopSum: Number(history.summary?.best_wins_top_sum ?? 0),
        strategyHints: Array.isArray(history.summary?.strategy_hints) ? history.summary!.strategy_hints! : [],
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载战报失败');
      setRows([]);
      setLogs([]);
      setSceneTop([]);
      setSummaryState({
        total: 0,
        wins: 0,
        losses: 0,
        winRate: 0,
        net: 0,
        feesTotal: 0,
        worstLosses: [],
        bestWins: [],
        worstTopSum: 0,
        bestTopSum: 0,
        strategyHints: [],
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 30000);
    return () => window.clearInterval(t);
  }, [load]);

  const summary = useMemo(() => {
    const last = rows[0];
    const bySymbol = new Map<string, number>();
    for (const r of rows) bySymbol.set(r.symbol, (bySymbol.get(r.symbol) || 0) + Number(r.net_pnl || 0));
    const top = [...bySymbol.entries()].sort((a, b) => b[1] - a[1])[0];
    return {
      total: summaryState.total,
      wins: summaryState.wins,
      losses: summaryState.losses,
      net: summaryState.net,
      winRate: summaryState.winRate,
      last,
      top,
    };
  }, [rows, summaryState]);

  const fmtTime = useCallback((ts: number) => {
    if (!Number.isFinite(ts) || ts <= 0) return '—';
    try {
      return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
    } catch {
      return '—';
    }
  }, []);

  return (
    <div className="ti-glass rounded-xl flex flex-col min-h-0 h-full overflow-hidden ring-1 ring-slate-200/80 shadow-sm">
      <div className="px-3 py-2 border-b border-slate-200/90 flex items-center justify-between gap-2 shrink-0 bg-gradient-to-r from-white to-slate-50/90">
        <div className="flex items-center gap-2">
          <ScrollText className="w-4 h-4 text-teal-600" />
          <span className="text-[10px] font-semibold tracking-wide text-slate-500 uppercase">战报摘要</span>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="p-1.5 rounded-lg border border-slate-200 text-slate-500 hover:text-amber-700 hover:border-amber-200/80 disabled:opacity-50 bg-white"
          title="刷新战报"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {error ? (
        <div className="m-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-800">
          {error}
        </div>
      ) : null}

      <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-3 text-[11px]">
        {summaryState.strategyHints.length > 0 ? (
          <div className="rounded-lg border border-amber-200/90 bg-amber-50/50 p-2.5 text-[10px] text-amber-950 leading-snug">
            <div className="text-amber-900/90 text-[9px] uppercase tracking-wider font-semibold mb-1">策略提示</div>
            <ul className="list-disc pl-3 space-y-0.5">
              {summaryState.strategyHints.map((t, i) => (
                <li key={i}>{t}</li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="grid grid-cols-2 gap-2">
          <div className="rounded-lg border border-slate-200/90 bg-white p-2 shadow-sm">
            <div className="text-slate-500 text-[9px] uppercase tracking-wider">近战总场次</div>
            <div className="font-mono text-slate-900 mt-1 tabular-nums">{summary.total}</div>
          </div>
          <div className="rounded-lg border border-slate-200/90 bg-white p-2 shadow-sm">
            <div className="text-slate-500 text-[9px] uppercase tracking-wider">胜率</div>
            <div className="font-mono text-teal-700 mt-1 tabular-nums">{fmtNum(summary.winRate)}%</div>
          </div>
          <div className="rounded-lg border border-slate-200/90 bg-white p-2 shadow-sm">
            <div className="text-slate-500 text-[9px] uppercase tracking-wider">累计净盈亏</div>
            <div className={`font-mono mt-1 tabular-nums ${summary.net >= 0 ? 'text-emerald-700' : 'text-rose-600'}`}>
              {summary.net >= 0 ? '+' : ''}
              {fmtNum(summary.net, 4)}
            </div>
          </div>
          <div className="rounded-lg border border-slate-200/90 bg-white p-2 shadow-sm">
            <div className="text-slate-500 text-[9px] uppercase tracking-wider">胜 / 负</div>
            <div className="font-mono text-slate-700 mt-1 tabular-nums">
              {summary.wins} / {summary.losses}
            </div>
          </div>
        </div>

        <div className="rounded-lg border border-slate-200/90 bg-white p-2.5 shadow-sm">
          <div className="text-slate-500 text-[9px] uppercase tracking-wider mb-2">最近一次平仓</div>
          {summary.last ? (
            <div className="space-y-1 text-slate-600 text-[11px]">
              <div className="font-mono text-slate-900">{summary.last.symbol}</div>
              <div>
                原因: <span className="text-slate-800">{exitReasonZh(summary.last.exit_reason)}</span>
              </div>
              <div className={summary.last.net_pnl >= 0 ? 'text-emerald-700' : 'text-rose-600'}>
                净盈亏 {summary.last.net_pnl >= 0 ? '+' : ''}
                {fmtNum(summary.last.net_pnl, 4)}
              </div>
            </div>
          ) : (
            <div className="text-slate-500">暂无平仓战报</div>
          )}
        </div>

        <div className="rounded-lg border border-slate-200/90 bg-white p-2.5 shadow-sm">
          <div className="text-slate-500 text-[9px] uppercase tracking-wider mb-2">火力贡献最高</div>
          {summary.top ? (
            <div className="font-mono text-amber-800 text-[11px]">
              {summary.top[0]} · {summary.top[1] >= 0 ? '+' : ''}
              {fmtNum(summary.top[1], 4)}
            </div>
          ) : (
            <div className="text-slate-500">暂无数据</div>
          )}
        </div>

        <div className="rounded-lg border border-rose-200/80 bg-rose-50/40 p-2.5 shadow-sm">
          <div className="flex items-baseline justify-between gap-2 mb-2">
            <div className="text-rose-700/90 text-[9px] uppercase tracking-wider font-semibold">亏损黑榜 Top10</div>
            <div className="text-[9px] text-rose-600/80 tabular-nums">合计 {fmtNum(summaryState.worstTopSum, 4)} USDT</div>
          </div>
          {summaryState.worstLosses.length > 0 ? (
            <div className="space-y-1.5 font-mono text-[10px] leading-snug">
              {summaryState.worstLosses.map((row: LeaderboardRow, idx: number) => (
                <div
                  key={`${row.file || row.symbol}-${idx}`}
                  className="border-b border-rose-100/90 pb-1.5 last:border-0 last:pb-0 text-slate-700"
                >
                  <div className="flex justify-between gap-2">
                    <span className="text-rose-700">#{idx + 1}</span>
                    <span className="text-slate-900 shrink-0">{row.symbol || '—'}</span>
                    <span className="text-rose-600 tabular-nums">{fmtNum(Number(row.net_pnl ?? 0), 4)}</span>
                  </div>
                  <div className="text-slate-500 mt-0.5 pl-4">
                    {exitReasonZh(String(row.exit_reason || ''))} · {row.side} · ×{fmtNum(Number(row.leverage ?? 0), 0)}
                  </div>
                  <div className="text-slate-400 pl-4">{fmtTime(Number(row.closed_at ?? 0))}</div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-slate-500 text-[10px]">暂无亏损平仓或未写入 autopsy</div>
          )}
        </div>

        <div className="rounded-lg border border-emerald-200/70 bg-emerald-50/30 p-2.5 shadow-sm">
          <div className="flex items-baseline justify-between gap-2 mb-2">
            <div className="text-emerald-800/90 text-[9px] uppercase tracking-wider font-semibold">盈利红榜 Top10</div>
            <div className="text-[9px] text-emerald-700/80 tabular-nums">合计 +{fmtNum(summaryState.bestTopSum, 4)} USDT</div>
          </div>
          {summaryState.bestWins.length > 0 ? (
            <div className="space-y-1.5 font-mono text-[10px] leading-snug">
              {summaryState.bestWins.map((row: LeaderboardRow, idx: number) => (
                <div
                  key={`${row.file || row.symbol}-w-${idx}`}
                  className="border-b border-emerald-100/80 pb-1.5 last:border-0 last:pb-0 text-slate-700"
                >
                  <div className="flex justify-between gap-2">
                    <span className="text-emerald-700">#{idx + 1}</span>
                    <span className="text-slate-900 shrink-0">{row.symbol || '—'}</span>
                    <span className="text-emerald-700 tabular-nums">+{fmtNum(Number(row.net_pnl ?? 0), 4)}</span>
                  </div>
                  <div className="text-slate-500 mt-0.5 pl-4">
                    {exitReasonZh(String(row.exit_reason || ''))} · {row.side}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-slate-500 text-[10px]">暂无盈利记录</div>
          )}
        </div>

        <div className="rounded-lg border border-slate-200/90 bg-white p-2.5 shadow-sm">
          <div className="text-slate-500 text-[9px] uppercase tracking-wider mb-2">主攻场景 Top</div>
          {sceneTop.length > 0 ? (
            <div className="space-y-1 font-mono text-[10px]">
              {sceneTop.map((scene: SceneLeaderboardItem, idx: number) => (
                <div key={`${idx}-${scene.scene_key || scene.symbol || 'scene'}`} className="text-slate-600">
                  <span className="text-teal-600">#{idx + 1}</span>{' '}
                  {scene.regime}/{scene.symbol}/{scene.side}/{scene.strategy}/Q{scene.quadrant} · {scene.ai_bucket}
                  <span className="text-slate-500"> · WR {fmtNum((Number(scene.wr ?? 0) || 0) * 100, 0)}%</span>
                  <span className={`${Number(scene.net ?? 0) >= 0 ? 'text-emerald-700' : 'text-rose-600'}`}>
                    {' '}
                    · {Number(scene.net ?? 0) >= 0 ? '+' : ''}
                    {fmtNum(Number(scene.net ?? 0), 4)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-slate-500">场景排行生成中</div>
          )}
        </div>

        <div className="rounded-lg border border-slate-200/80 bg-slate-50 p-2.5">
          <div className="text-slate-500 text-[9px] uppercase tracking-wider mb-2">最新战斗日志</div>
          <div className="space-y-1 font-mono text-[9px] leading-snug">
            {logs.length > 0 ? (
              logs.map((line, i) => (
                <div key={`${i}-${line.slice(0, 18)}`} className="border-b border-slate-200/60 pb-1 text-slate-600">
                  {line}
                </div>
              ))
            ) : (
              <div className="text-slate-500">暂无系统日志</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
