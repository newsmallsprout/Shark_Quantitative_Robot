import React from 'react';
import { useStore } from '../store/useStore';
import { Wallet, TrendingUp, Activity, ShieldAlert } from 'lucide-react';

const KPICards: React.FC = () => {
  const { equity, dailyPnl, dailyPnlPercent, mode, isRunning } = useStore();
  const isPositive = dailyPnl >= 0;

  const modeLabel =
    mode === 'NEUTRAL' ? '观望' : mode === 'ATTACK' ? '潜伏' : mode === 'BERSERKER' ? '狂暴' : '熔断';

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
      <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 flex items-center justify-between gap-2">
        <div>
          <p className="text-[9px] text-[#5c6578] uppercase tracking-widest mb-0.5">总权益</p>
          <h3 className="text-lg font-bold font-mono text-[#e8eaef]">
            ${equity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </h3>
        </div>
        <Wallet className="w-5 h-5 text-[#e7b15a]/70 shrink-0" />
      </div>

      <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 flex items-center justify-between gap-2">
        <div>
          <p className="text-[9px] text-[#5c6578] uppercase tracking-widest mb-0.5">已实现净盈亏</p>
          <div className="flex items-baseline gap-1.5 flex-wrap">
            <h3 className={`text-lg font-bold font-mono ${isPositive ? 'ti-profit' : 'ti-loss'}`}>
              {isPositive ? '+' : ''}${dailyPnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </h3>
            <span className={`text-xs font-mono ${isPositive ? 'ti-profit' : 'ti-loss'} opacity-80`}>
              ({isPositive ? '+' : ''}
              {dailyPnlPercent.toFixed(2)}%)
            </span>
          </div>
        </div>
        <TrendingUp className={`w-5 h-5 shrink-0 ${isPositive ? 'text-[#2dd4bf]/75' : 'text-[#f43f5e]/75'}`} />
      </div>

      <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 flex items-center justify-between gap-2">
        <div>
          <p className="text-[9px] text-[#5c6578] uppercase tracking-widest mb-0.5">战术姿态</p>
          <h3
            className={`text-lg font-bold ${
              mode === 'BERSERKER' ? 'ti-loss animate-pulse' : mode === 'ATTACK' ? 'ti-warn' : 'text-[#e7b15a]'
            }`}
          >
            {modeLabel}
          </h3>
        </div>
        <Activity
          className={`w-5 h-5 shrink-0 ${
            mode === 'BERSERKER' ? 'text-[#f43f5e]' : mode === 'ATTACK' ? 'text-[#e7b15a]' : 'text-[#2dd4bf]'
          }`}
        />
      </div>

      <div className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2 flex items-center justify-between gap-2">
        <div>
          <p className="text-[9px] text-[#5c6578] uppercase tracking-widest mb-0.5">引擎链路</p>
          <div className="flex items-center gap-2">
            <div
              className={`w-2 h-2 rounded-full ${isRunning ? 'bg-[#2dd4bf] shadow-[0_0_8px_rgba(45,212,191,0.45)]' : 'bg-[#f43f5e]'}`}
            />
            <h3 className={`text-lg font-bold font-mono ${isRunning ? 'ti-profit' : 'ti-loss'}`}>
              {isRunning ? '在线' : '离线'}
            </h3>
          </div>
        </div>
        <ShieldAlert className={`w-5 h-5 shrink-0 ${isRunning ? 'text-[#2dd4bf]/65' : 'text-[#f43f5e]/65'}`} />
      </div>
    </div>
  );
};

export default KPICards;
