import React from 'react';
import { useStore } from '../store/useStore';
import { BrainCircuit, Info } from 'lucide-react';

const AIInsights: React.FC = () => {
  const { aiInsight } = useStore();

  if (!aiInsight) return null;

  // Determine colors based on regime
  const getRegimeColor = (regime: string) => {
    switch (regime) {
      case 'TRENDING_UP': return 'text-emerald-400';
      case 'TRENDING_DOWN': return 'text-red-400';
      case 'OSCILLATING': return 'text-blue-400';
      case 'CHAOTIC': return 'text-orange-400';
      default: return 'text-slate-400';
    }
  };

  return (
    <div className="card flex flex-col h-full">
      <div className="flex items-center justify-between mb-4 pb-4 border-b border-slate-700">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <BrainCircuit className="w-5 h-5 text-purple-400" />
          LLM Market Insights
        </h2>
        <span className="text-xs text-slate-500 font-mono">
          Last updated: {new Date(aiInsight.timestamp).toLocaleTimeString()}
        </span>
      </div>
      
      <div className="flex-1 grid grid-cols-2 gap-4 mb-4">
        {/* Regime */}
        <div className="bg-slate-800/50 p-4 rounded-lg border border-slate-700/50">
          <p className="text-xs text-slate-400 uppercase tracking-wider mb-2">Market Regime</p>
          <div className={`text-xl font-bold ${getRegimeColor(aiInsight.regime)}`}>
            {aiInsight.regime.replace('_', ' ')}
          </div>
        </div>

        {/* Score */}
        <div className="bg-slate-800/50 p-4 rounded-lg border border-slate-700/50">
          <p className="text-xs text-slate-400 uppercase tracking-wider mb-2">Bullish Score</p>
          <div className="flex items-end gap-2">
            <div className={`text-3xl font-bold ${aiInsight.score > 50 ? 'text-emerald-400' : 'text-red-400'}`}>
              {aiInsight.score.toFixed(1)}
            </div>
            <span className="text-sm text-slate-500 mb-1">/ 100</span>
          </div>
          {/* Simple progress bar */}
          <div className="w-full bg-slate-700 h-1.5 mt-3 rounded-full overflow-hidden">
            <div 
              className={`h-full ${aiInsight.score > 50 ? 'bg-emerald-500' : 'bg-red-500'}`} 
              style={{ width: `${aiInsight.score}%` }} 
            />
          </div>
        </div>
      </div>

      {/* Analysis Summary */}
      <div className="bg-blue-500/5 p-4 rounded-lg border border-blue-500/20 flex gap-3">
        <Info className="w-5 h-5 text-blue-400 shrink-0 mt-0.5" />
        <p className="text-sm text-slate-300 leading-relaxed">
          <span className="font-semibold text-blue-300 mr-2">Analysis:</span>
          {aiInsight.reason}
        </p>
      </div>
    </div>
  );
};

export default AIInsights;
