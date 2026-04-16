import React from 'react';
import { useStore } from '../../store/useStore';

function fmtPx(n: number | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (n >= 1) return n.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return n.toPrecision(4);
}

export const TacticalCenter: React.FC<{ compact?: boolean }> = ({ compact = false }) => {
  const { activeSymbol, contractSpecs, resonanceMetrics, latestTick } = useStore();
  const spec = contractSpecs[activeSymbol];
  const last = spec?.last_price;
  const displayLast = latestTick?.symbol === activeSymbol ? latestTick.price : last;

  const obi = resonanceMetrics.obi;
  const obiPct = Math.max(0, Math.min(100, ((obi + 1) / 2) * 100));
  const tech = resonanceMetrics.tech_signal;
  const techCls =
    tech === 'bullish' ? 'text-teal-600' : tech === 'bearish' ? 'text-rose-600' : 'text-slate-500';

  const regime = resonanceMetrics.ai_regime || '—';
  const adaptation = resonanceMetrics.adaptation;
  const regimeCls =
    regime.includes('UP') || regime === 'TRENDING_UP'
      ? 'text-teal-700'
      : regime.includes('DOWN') || regime === 'TRENDING_DOWN'
        ? 'text-rose-700'
        : regime === 'CHAOTIC'
          ? 'text-amber-600'
          : 'text-slate-600';

  return (
    <div className="ti-glass rounded-xl flex flex-col min-h-0 h-full overflow-hidden ring-1 ring-slate-200/80 shadow-sm">
      <div className="px-3 py-2 border-b border-slate-200/90 flex items-center justify-between gap-2 shrink-0 bg-gradient-to-r from-white to-slate-50/90">
        <div className="flex items-center gap-2 min-w-0">
          <div className="min-w-0">
            <div className="text-[11px] font-semibold tracking-wide text-slate-500 uppercase">战术读数</div>
            <div className="text-xs font-mono text-slate-900 font-medium truncate">{activeSymbol}</div>
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className="text-[9px] text-slate-500 uppercase tracking-wider">Last</div>
          <div className="text-sm font-mono font-semibold text-amber-700 tabular-nums">{fmtPx(displayLast)}</div>
        </div>
      </div>

      <div
        className={
          compact
            ? 'flex-1 min-h-0 p-2 flex flex-wrap gap-2 items-stretch content-start ti-matrix-scroll overflow-y-auto'
            : 'flex-1 min-h-0 overflow-y-auto p-3 space-y-3 ti-matrix-scroll'
        }
      >
        <div className={compact ? 'flex flex-1 min-w-[200px] gap-2 flex-wrap' : 'grid grid-cols-2 gap-2'}>
          <div className="rounded-lg border border-slate-200/90 bg-white shadow-sm px-2.5 py-2 flex-1 min-w-[120px]">
            <div className="text-[9px] text-slate-500 uppercase tracking-wider mb-1">盘口价差</div>
            <div className="text-xs font-mono text-slate-800">
              {spec?.best_bid != null && spec?.best_ask != null ? (
                <>
                  <span className="text-teal-600">{fmtPx(spec.best_bid)}</span>
                  <span className="text-slate-400 mx-1">/</span>
                  <span className="text-rose-600">{fmtPx(spec.best_ask)}</span>
                </>
              ) : (
                '—'
              )}
            </div>
            <div className="text-[10px] font-mono text-slate-500 mt-0.5">
              spread {(spec?.spread != null ? (spec.spread * 10000).toFixed(2) : '—')} bps
            </div>
          </div>
          <div className="rounded-lg border border-slate-200/90 bg-white shadow-sm px-2.5 py-2 flex-1 min-w-[120px]">
            <div className="text-[9px] text-slate-500 uppercase tracking-wider mb-1">OBI</div>
            <div className="h-1.5 rounded-full bg-slate-200 overflow-hidden mb-1">
              <div
                className="h-full rounded-full bg-gradient-to-r from-rose-400 via-slate-400 to-teal-500"
                style={{ width: `${obiPct}%` }}
              />
            </div>
            <div className="text-xs font-mono text-slate-800">{obi.toFixed(3)}</div>
          </div>
        </div>

        {!compact ? (
          <div className="rounded-lg border border-slate-200/90 bg-white shadow-sm px-2.5 py-2">
            <div className="text-[9px] text-slate-500 uppercase tracking-wider mb-1.5">共振 / 体制</div>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className={`text-sm font-bold ${regimeCls}`}>{regime.replace(/_/g, ' ')}</span>
              <span className="text-xs font-mono text-slate-500">AI {resonanceMetrics.ai_score.toFixed(0)}</span>
              <span className={`text-xs font-semibold uppercase ${techCls}`}>{tech}</span>
              <span className="text-[10px] font-mono text-slate-500">
                ATR {(resonanceMetrics.atr_pct * 100).toFixed(2)}%
              </span>
            </div>
            {resonanceMetrics.ai_reason ? (
              <p className="text-[11px] text-slate-600 leading-snug mt-2 line-clamp-4">
                {resonanceMetrics.ai_reason}
              </p>
            ) : null}
          </div>
        ) : (
          <div className="rounded-lg border border-slate-200/90 bg-white shadow-sm px-2.5 py-2 flex flex-wrap items-center gap-x-3 gap-y-1 min-w-[180px] flex-1">
            <span className={`text-xs font-bold ${regimeCls}`}>{regime.replace(/_/g, ' ')}</span>
            <span className="text-[10px] font-mono text-slate-500">AI {resonanceMetrics.ai_score.toFixed(0)}</span>
            <span className={`text-[10px] font-semibold uppercase ${techCls}`}>{tech}</span>
            <span className="text-[10px] font-mono text-slate-500">
              ATR {(resonanceMetrics.atr_pct * 100).toFixed(2)}%
            </span>
          </div>
        )}

        <div className="rounded-lg border border-slate-200/90 bg-white shadow-sm px-2.5 py-2">
          <div className="text-[9px] text-slate-500 uppercase tracking-wider mb-1.5">自适应战术</div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span
              className={`text-xs font-bold ${
                adaptation.adaptation_level >= 2
                  ? 'text-rose-600'
                  : adaptation.adaptation_level === 1
                    ? 'text-amber-600'
                    : 'text-teal-600'
              }`}
            >
              {adaptation.adaptation_label}
            </span>
            <span className="text-[10px] font-mono text-slate-500">
              WR {(adaptation.window_win_rate * 100).toFixed(0)}%
            </span>
            <span className="text-[10px] font-mono text-slate-500">L {adaptation.consecutive_losses}</span>
            <span className="text-[10px] font-mono text-slate-500">
              A{'>'}
              {adaptation.live_attack_ai_threshold.toFixed(0)}
            </span>
            <span className="text-[10px] font-mono text-slate-500">
              N{'>'}
              {adaptation.live_neutral_ai_threshold.toFixed(0)}
            </span>
            <span className="text-[10px] font-mono text-slate-500">
              Margin {adaptation.live_margin_cap_usdt.toFixed(1)}
            </span>
          </div>
          <p className="text-[10px] text-slate-500 mt-1.5">
            {adaptation.probe_mode ? '侦察模式开启，AI 置信度已打折。' : '当前按常规火力执行。'}
          </p>
        </div>

        <div className="rounded-lg border border-slate-200/90 bg-white shadow-sm px-2.5 py-2">
          <div className="text-[9px] text-slate-500 uppercase tracking-wider mb-1.5">套利监控</div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] font-mono text-slate-600">
            <span>Anchor {resonanceMetrics.beta_neutral_hf.anchor_symbol || 'BTC/USDT'}</span>
            <span>套利杠杆 {resonanceMetrics.beta_neutral_hf.configured_leverage || 0}x</span>
            <span>候选 {resonanceMetrics.beta_neutral_hf.candidate_pairs.length}</span>
            <span>活跃 {resonanceMetrics.beta_neutral_hf.active_pairs.length}</span>
            <span>
              BTC 对冲 {resonanceMetrics.beta_neutral_hf.anchor_actual_contracts.toFixed(3)} /{' '}
              {resonanceMetrics.beta_neutral_hf.anchor_target_contracts.toFixed(3)}
            </span>
          </div>
          {(resonanceMetrics.beta_neutral_hf.leg_micro_dynamic_floor_usdt ?? 0) > 0 ||
          (resonanceMetrics.beta_neutral_hf.decoupled_margin_loss_cap ?? 0) > 0 ? (
            <p className="text-[10px] text-slate-600 mt-1.5 leading-snug">
              配置：微利止盈净利地板 ≥{' '}
              {(resonanceMetrics.beta_neutral_hf.leg_micro_dynamic_floor_usdt ??
                resonanceMetrics.beta_neutral_hf.leg_micro_take_usdt ??
                0
              ).toFixed(2)}{' '}
              USDT · 脱钩腿浮亏上限 ≈ 保证金 ×{' '}
              {((resonanceMetrics.beta_neutral_hf.decoupled_margin_loss_cap ?? 0) * 100).toFixed(0)}%
            </p>
          ) : null}
          {resonanceMetrics.beta_neutral_hf.candidate_pairs.length > 0 ? (
            <div className="mt-2 space-y-1 text-[10px] font-mono">
              {resonanceMetrics.beta_neutral_hf.candidate_pairs.slice(0, compact ? 3 : 5).map((p) => (
                <div key={p.alt} className="text-slate-600">
                  候选 {p.alt} · z {p.zscore.toFixed(2)} · beta {p.beta.toFixed(2)} · corr {p.corr.toFixed(2)} · {p.direction}
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-2 text-[10px] text-slate-500">候选对生成中</div>
          )}
          {resonanceMetrics.beta_neutral_hf.active_pairs.length > 0 ? (
            <div className="mt-2 space-y-1 text-[10px] font-mono">
              {resonanceMetrics.beta_neutral_hf.active_pairs.map((p) => (
                <div key={p.pair_id} className="text-teal-700">
                  持仓 {p.alt} · {p.effective_leverage}x · z {Number(p.live_zscore).toFixed(2)} · 净{' '}
                  {p.net_pnl_usdt >= 0 ? '+' : ''}
                  {Number(p.net_pnl_usdt).toFixed(3)}
                  {(p.dynamic_take_profit_usdt != null && p.dynamic_take_profit_usdt > 0) ||
                  (p.dynamic_stop_loss_usdt != null && p.dynamic_stop_loss_usdt > 0) ? (
                    <>
                      {' '}
                      · 止盈≥{Number(p.dynamic_take_profit_usdt).toFixed(2)}U · 止损≈
                      {Number(p.dynamic_stop_loss_usdt).toFixed(2)}U
                    </>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}
          {resonanceMetrics.beta_neutral_hf.recent_closed.length > 0 ? (
            <div className="mt-2 space-y-1 text-[10px] font-mono">
              {resonanceMetrics.beta_neutral_hf.recent_closed.slice(0, compact ? 2 : 4).map((p) => (
                <div key={`${p.pair_id}-${p.closed_at}`} className="text-slate-500">
                  平仓 {p.alt}/{p.anchor} · {p.reason} · {p.net_pnl >= 0 ? '+' : ''}
                  {p.net_pnl.toFixed(4)}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};
