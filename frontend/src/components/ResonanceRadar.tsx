import React from 'react';
import { useStore } from '../store/useStore';
import { Radar, Target, BrainCircuit, Activity } from 'lucide-react';

const ResonanceRadar: React.FC = () => {
  const { resonanceMetrics } = useStore();
  
  if (!resonanceMetrics) return null;

  // OBI Normalization (-1 to 1) -> (0% to 100%)
  const obiPercent = ((resonanceMetrics.obi + 1) / 2) * 100;
  const obiColor = resonanceMetrics.obi > 0.3 ? 'bg-emerald-500' : resonanceMetrics.obi < -0.3 ? 'bg-red-500' : 'bg-slate-500';

  // Tech Signal
  const techColor = resonanceMetrics.tech_signal === 'bullish' ? 'text-emerald-400' : resonanceMetrics.tech_signal === 'bearish' ? 'text-red-400' : 'text-slate-400';

  // Resonance Lights
  const aiBullish = resonanceMetrics.ai_score >= 60;
  const aiBearish = resonanceMetrics.ai_score <= 40;
  
  const techBullish = resonanceMetrics.tech_signal === 'bullish';
  const techBearish = resonanceMetrics.tech_signal === 'bearish';
  
  const obiBullish = resonanceMetrics.obi > 0.3;
  const obiBearish = resonanceMetrics.obi < -0.3;

  const getLightColor = (bullish: boolean, bearish: boolean) => {
    if (bullish) return 'bg-emerald-500 shadow-[0_0_10px_rgba(16,185,129,0.5)]';
    if (bearish) return 'bg-red-500 shadow-[0_0_10px_rgba(239,68,68,0.5)]';
    return 'bg-slate-700';
  };

  const isResonating = (aiBullish && techBullish && obiBullish) || (aiBearish && techBearish && obiBearish);

  const regimeColor =
    resonanceMetrics.ai_regime === 'TRENDING_UP'
      ? 'text-emerald-400'
      : resonanceMetrics.ai_regime === 'TRENDING_DOWN'
        ? 'text-red-400'
        : resonanceMetrics.ai_regime === 'CHAOTIC'
          ? 'text-orange-400'
          : 'text-blue-400';

  return (
    <div className="card flex flex-col h-full border-slate-700">
      <div className="flex items-center justify-between mb-4 pb-4 border-b border-slate-700">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <Radar className="w-5 h-5 text-blue-400" />
          Resonance Radar
        </h2>
        {isResonating && (
          <span className="animate-pulse bg-purple-500/20 text-purple-400 px-2 py-1 rounded text-xs font-mono border border-purple-500/50">
            SIGNAL TRIGGERED
          </span>
        )}
      </div>

      <div className="space-y-6">
        {/* Signal Status Lights */}
        <div>
          <p className="text-xs text-slate-400 uppercase tracking-wider mb-3">Resonance Matrix</p>
          <div className="flex justify-between items-center bg-slate-800/50 p-3 rounded-lg border border-slate-700/50">
            <div className="flex flex-col items-center gap-2">
              <BrainCircuit className="w-4 h-4 text-slate-400" />
              <div className={`w-3 h-3 rounded-full ${getLightColor(aiBullish, aiBearish)}`} />
              <span className="text-[10px] text-slate-500">AI (Macro)</span>
            </div>
            <div className="h-px bg-slate-700 flex-1 mx-4" />
            <div className="flex flex-col items-center gap-2">
              <Activity className="w-4 h-4 text-slate-400" />
              <div className={`w-3 h-3 rounded-full ${getLightColor(techBullish, techBearish)}`} />
              <span className="text-[10px] text-slate-500">Tech (Mid)</span>
            </div>
            <div className="h-px bg-slate-700 flex-1 mx-4" />
            <div className="flex flex-col items-center gap-2">
              <Target className="w-4 h-4 text-slate-400" />
              <div className={`w-3 h-3 rounded-full ${getLightColor(obiBullish, obiBearish)}`} />
              <span className="text-[10px] text-slate-500">OBI (Micro)</span>
            </div>
          </div>
        </div>

        {/* OBI Level */}
        <div>
          <div className="flex justify-between text-xs mb-2">
            <span className="text-slate-400 uppercase tracking-wider">Order Book Imbalance (L2)</span>
            <span className="font-mono text-slate-300">{resonanceMetrics.obi.toFixed(2)}</span>
          </div>
          <div className="relative w-full h-2 bg-slate-700 rounded-full overflow-hidden">
            {/* Center line */}
            <div className="absolute left-1/2 top-0 bottom-0 w-px bg-slate-400 z-10" />
            <div 
              className={`absolute top-0 bottom-0 ${obiColor} transition-all duration-300`}
              style={{ 
                left: resonanceMetrics.obi < 0 ? `${obiPercent}%` : '50%',
                right: resonanceMetrics.obi > 0 ? `${100 - obiPercent}%` : '50%'
              }}
            />
          </div>
          <div className="flex justify-between text-[10px] text-slate-500 mt-1 font-mono">
            <span>-1.0 (SELL WALL)</span>
            <span>0.0</span>
            <span>+1.0 (BUY WALL)</span>
          </div>
        </div>

        {/* Tech Indicator */}
        <div className="flex items-center justify-between bg-slate-800/30 p-3 rounded-lg">
          <span className="text-sm text-slate-400">SMA Breakout Status</span>
          <span className={`font-bold uppercase ${techColor}`}>
            {resonanceMetrics.tech_signal} ({(resonanceMetrics.tech_indicator * 100).toFixed(2)}%)
          </span>
        </div>

        <div className="bg-slate-800/40 p-3 rounded-lg border border-slate-700/50">
          <p className="text-[10px] text-slate-500 uppercase tracking-wider mb-1">当前品种 LLM 盘面（周期更新）</p>
          <p className={`text-sm font-semibold ${regimeColor}`}>
            {resonanceMetrics.ai_regime.replaceAll('_', ' ')}
          </p>
          {resonanceMetrics.ai_reason ? (
            <p className="text-xs text-slate-400 mt-2 leading-relaxed">{resonanceMetrics.ai_reason}</p>
          ) : (
            <p className="text-xs text-slate-500 mt-2">等待异步研究员推送该合约的解读…</p>
          )}
        </div>
      </div>
    </div>
  );
};

export default ResonanceRadar;