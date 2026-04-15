import React from 'react';
import { useStore } from '../store/useStore';
import { ShieldAlert, TrendingDown, Scale, Flame } from 'lucide-react';

const DynamicRiskPanel: React.FC = () => {
  const { resonanceMetrics, mode, activeSymbol } = useStore();

  if (!resonanceMetrics) return null;

  const maxSingleRisk = 5.0;
  const currentRisk = resonanceMetrics.current_risk_exposure;
  const riskPercent = Math.min((currentRisk / maxSingleRisk) * 100, 100);
  const riskColor =
    currentRisk > maxSingleRisk * 0.8 ? 'bg-red-500' : currentRisk > maxSingleRisk * 0.5 ? 'bg-orange-500' : 'bg-emerald-500';

  const atrPct = resonanceMetrics.atr_pct;
  const atrValue = (Math.min(atrPct, 1) * 100).toFixed(2);
  const targetLeverage = resonanceMetrics.target_leverage;
  const berserkCap = Math.round(resonanceMetrics.berserker_max_leverage || 0);

  return (
    <div className="card flex flex-col h-full border-slate-700">
      <div className="flex items-center justify-between mb-4 pb-4 border-b border-slate-700">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <ShieldAlert className="w-5 h-5 text-orange-400" />
          动态风险与仓位
        </h2>
        <span
          className={`px-2 py-1 rounded text-xs font-mono border ${
            mode === 'BERSERKER'
              ? 'border-red-500/60 text-red-400 bg-red-500/20 animate-pulse'
              : mode === 'ATTACK'
                ? 'border-purple-500/50 text-purple-400 bg-purple-500/20'
                : 'border-blue-500/50 text-blue-400 bg-blue-500/20'
          }`}
        >
          {mode}
        </span>
      </div>

      <div className="space-y-6">
        <div>
          <div className="flex justify-between text-xs mb-2">
            <span className="text-slate-400 uppercase tracking-wider font-medium">单笔保证金占用 / 5% 参考</span>
            <span className="font-mono text-slate-300">
              {currentRisk.toFixed(2)}% / {maxSingleRisk.toFixed(1)}%
            </span>
          </div>
          <div className="relative w-full h-3 bg-slate-800 rounded-full overflow-hidden border border-slate-700">
            <div
              className={`absolute top-0 left-0 bottom-0 ${riskColor} transition-all duration-500`}
              style={{ width: `${riskPercent}%` }}
            />
          </div>
          <p className="text-[10px] text-slate-500 mt-1">
            狂暴模式可满仓逐仓，此条为相对权益的估算占用；单日 Grinder 单笔仍受 RiskEngine 5% 约束。
          </p>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="bg-slate-800/50 p-4 rounded-lg border border-slate-700/50">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs text-slate-400 uppercase tracking-wider">波动 ATR</span>
              <TrendingDown className="w-4 h-4 text-slate-500" />
            </div>
            <div className="text-2xl font-bold text-slate-200">{atrValue}%</div>
            <p className="text-[10px] text-slate-500 mt-1">相对振幅（已截断显示 ≤100%）</p>
          </div>

          <div
            className={`p-4 rounded-lg border ${
              mode === 'BERSERKER'
                ? 'bg-red-950/40 border-red-600/50'
                : 'bg-slate-800/50 border-slate-700/50'
            }`}
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs text-slate-400 uppercase tracking-wider">
                {mode === 'BERSERKER' ? '狂暴档位上限' : '目标杠杆'}
              </span>
              {mode === 'BERSERKER' ? (
                <Flame className="w-4 h-4 text-red-400" />
              ) : (
                <Scale className="w-4 h-4 text-slate-500" />
              )}
            </div>
            {mode === 'BERSERKER' ? (
              <>
                <div className="flex items-baseline gap-1">
                  <div className="text-2xl font-bold text-red-400">{berserkCap}x</div>
                  <span className="text-xs text-slate-500">({activeSymbol})</span>
                </div>
                <p className="text-[10px] text-slate-500 mt-1">
                  OBI 触发后纸面单使用此档 cap；左侧「目标杠杆」为 Grinder Kelly，不代表狂暴实盘倍数。
                </p>
              </>
            ) : (
              <>
                <div className="flex items-baseline gap-1">
                  <div className="text-2xl font-bold text-emerald-400">{targetLeverage}x</div>
                  <span className="text-xs text-slate-500">(Kelly)</span>
                </div>
                <p className="text-[10px] text-slate-500 mt-1">随 ATR 与半凯利动态缩放（10–20x 区间）</p>
              </>
            )}
          </div>
        </div>

        <div className="bg-slate-800/30 p-3 rounded-lg border border-slate-700/30 text-xs text-slate-400 leading-relaxed">
          <span className="font-semibold text-slate-300">逻辑说明：</span>
          {mode === 'BERSERKER' ? (
            <>
              狂暴路径绕过 AI，仅看盘口 OBI 极值 + Post-Only；杠杆取品种档位上限（见右栏），与 Kelly 面板解耦。
            </>
          ) : (
            <>
              Grinder 使用半凯利并结合 ATR 逆缩放；波动抬升时建议杠杆下移以保护本金（当前 ATR 约 {atrValue}%）。
            </>
          )}
        </div>
      </div>
    </div>
  );
};

export default DynamicRiskPanel;
