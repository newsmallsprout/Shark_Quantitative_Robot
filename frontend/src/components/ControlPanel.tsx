import React, { useState } from 'react';
import { apiFetch } from '../apiClient';
import { useStore } from '../store/useStore';
import type { SystemMode } from '../store/useStore';
import { AlertOctagon, Settings2, ShieldCheck, Zap, ActivitySquare, AlertTriangle, Flame } from 'lucide-react';

const ControlPanel: React.FC = () => {
  const { mode, setMode, killSwitch, isRunning, toggleEngine, resonanceMetrics } = useStore();
  const [showConfirm, setShowConfirm] = useState(false);

  const handleModeChange = async (newMode: SystemMode) => {
    if (newMode === mode || newMode === 'HALTED') return;
    try {
      const res = await apiFetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'SET_TRADING_MODE', mode: newMode }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        console.error('SET_TRADING_MODE failed', res.status, err);
        return;
      }
      setMode(newMode);
    } catch (e) {
      console.error('SET_TRADING_MODE', e);
    }
  };

  const handleKill = () => {
    killSwitch();
    setShowConfirm(false);
  };

  const ksProgress = resonanceMetrics?.kill_switch_progress;

  return (
    <div className="card flex flex-col h-full border-slate-700">
      <div className="flex items-center justify-between mb-4 pb-4 border-b border-slate-700">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <Settings2 className="w-5 h-5 text-slate-400" />
          Terminal Controls & Telemetry
        </h2>
      </div>

      {/* Mode Switches */}
      <div className="mb-6">
        <p className="text-sm text-slate-400 mb-3 uppercase tracking-wider font-medium">Trading Strategy Mode</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <button
            onClick={() => handleModeChange('NEUTRAL')}
            disabled={!isRunning}
            className={`btn ${
              mode === 'NEUTRAL' 
                ? 'bg-blue-600 text-white border-blue-500' 
                : 'bg-slate-800 text-slate-400 border-slate-700 hover:bg-slate-700'
            } border ${!isRunning && 'opacity-50 cursor-not-allowed'}`}
          >
            <ShieldCheck className="w-4 h-4" />
            Neutral Mode
          </button>
          
          <button
            onClick={() => handleModeChange('ATTACK')}
            disabled={!isRunning}
            className={`btn ${
              mode === 'ATTACK' 
                ? 'bg-purple-600 text-white border-purple-500 shadow-[0_0_15px_rgba(147,51,234,0.3)]' 
                : 'bg-slate-800 text-slate-400 border-slate-700 hover:bg-slate-700'
            } border ${!isRunning && 'opacity-50 cursor-not-allowed'}`}
          >
            <Zap className="w-4 h-4" />
            Attack Mode
          </button>

          <button
            onClick={() => handleModeChange('BERSERKER')}
            disabled={!isRunning}
            className={`btn ${
              mode === 'BERSERKER'
                ? 'bg-red-700 text-white border-red-500 shadow-[0_0_18px_rgba(220,38,38,0.45)] animate-pulse'
                : 'bg-slate-800 text-slate-400 border-slate-700 hover:bg-slate-700'
            } border ${!isRunning && 'opacity-50 cursor-not-allowed'}`}
          >
            <Flame className="w-4 h-4" />
            Berserker
          </button>
        </div>
      </div>

      {/* Execution Telemetry */}
      <div className="mb-6 flex-1">
        <p className="text-sm text-slate-400 mb-3 uppercase tracking-wider font-medium">Gateway Health</p>
        <div className="bg-slate-800/50 rounded-lg border border-slate-700 p-3 grid grid-cols-2 gap-4">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <ActivitySquare className="w-4 h-4 text-emerald-400" />
              <span className="text-xs text-slate-400">WS Ping</span>
            </div>
            <div className="font-mono text-lg text-slate-200">
              {resonanceMetrics?.ws_latency || '< 1'} <span className="text-xs text-slate-500">ms</span>
            </div>
          </div>
          <div>
            <div className="flex items-center gap-2 mb-1">
              <AlertTriangle className="w-4 h-4 text-orange-400" />
              <span className="text-xs text-slate-400">Reconnects</span>
            </div>
            <div className="font-mono text-lg text-slate-200">
              {resonanceMetrics?.ws_reconnects || 0} <span className="text-xs text-slate-500">24h</span>
            </div>
          </div>
        </div>
      </div>

      {/* Engine Toggle & Kill Switch */}
      <div className="space-y-3 mt-auto pt-4 border-t border-slate-700">
        {/* TWAP Kill Switch Progress */}
        {ksProgress?.active && (
          <div className="mb-4 bg-red-900/20 border border-red-500/50 p-3 rounded-lg">
            <div className="flex justify-between text-xs text-red-400 mb-2 font-mono">
              <span>TWAP CLOSE EXECUTION</span>
              <span>{ksProgress.executed_chunks} / {ksProgress.total_chunks} Chunks</span>
            </div>
            <div className="w-full h-2 bg-slate-800 rounded-full overflow-hidden">
              <div 
                className="h-full bg-red-500 transition-all duration-300"
                style={{ width: `${(ksProgress.executed_chunks / ksProgress.total_chunks) * 100}%` }}
              />
            </div>
            <p className="text-[10px] text-red-400/80 mt-2 text-center">Avg Slippage: {ksProgress.avg_slippage.toFixed(2)}%</p>
          </div>
        )}

        <button
          onClick={toggleEngine}
          className={`w-full py-3 rounded-lg font-bold tracking-wide transition-colors border ${
            isRunning 
              ? 'bg-slate-800 text-slate-300 border-slate-600 hover:bg-slate-700' 
              : 'bg-emerald-600 text-white border-emerald-500 hover:bg-emerald-500 shadow-[0_0_15px_rgba(16,185,129,0.3)]'
          }`}
        >
          {isRunning ? 'PAUSE ENGINE' : 'START ENGINE'}
        </button>

        {showConfirm ? (
          <div className="flex gap-2">
            <button 
              onClick={() => setShowConfirm(false)}
              className="flex-1 py-3 bg-slate-700 text-slate-300 rounded-lg hover:bg-slate-600 border border-slate-600"
            >
              CANCEL
            </button>
            <button 
              onClick={handleKill}
              className="flex-1 py-3 bg-red-600 text-white font-bold rounded-lg hover:bg-red-500 border border-red-500 shadow-[0_0_20px_rgba(239,68,68,0.5)] animate-pulse"
            >
              CONFIRM KILL
            </button>
          </div>
        ) : (
          <button
            onClick={() => setShowConfirm(true)}
            disabled={!isRunning && mode === 'HALTED'}
            className="w-full flex items-center justify-center gap-2 py-4 bg-red-900/40 text-red-400 font-bold rounded-lg border border-red-900 hover:bg-red-900/60 hover:text-red-300 transition-all uppercase tracking-widest disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <AlertOctagon className="w-5 h-5" />
            Smart Kill Switch
          </button>
        )}
      </div>
    </div>
  );
};

export default ControlPanel;
