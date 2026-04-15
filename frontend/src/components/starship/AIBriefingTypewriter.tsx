import React from 'react';
import { useStore } from '../../store/useStore';
import { Sparkles } from 'lucide-react';

const REGIME_ZH: Record<string, string> = {
  OSCILLATING: '震荡',
  TRENDING_UP: '上行',
  TRENDING_DOWN: '下行',
  CHAOTIC: '高波',
};

function modeLabel(mode: string): string {
  if (mode === 'ATTACK') return '进攻';
  if (mode === 'BERSERKER') return '狂暴';
  if (mode === 'HALTED') return '熔断';
  return '观望';
}

/** 结构化简报：取消打字机与整段 monospace，减少 WS 抖动带来的重播 */
export const AIBriefingTypewriter: React.FC = () => {
  const mode = useStore((s) => s.mode);
  const ai_regime = useStore((s) => s.resonanceMetrics.ai_regime);
  const ai_score = useStore((s) => s.resonanceMetrics.ai_score);
  const ai_reason = useStore((s) => s.resonanceMetrics.ai_reason);
  const aiInsight = useStore((s) => s.aiInsight);

  const regime = ai_regime || aiInsight?.regime || 'OSCILLATING';
  const score = Math.max(0, Math.min(100, Number(ai_score ?? aiInsight?.score ?? 50)));
  const reason = String(ai_reason || aiInsight?.reason || '').trim();

  const regimeZh = REGIME_ZH[regime] ?? regime;

  return (
    <div className="ti-glass rounded-lg flex flex-col h-full min-h-0 shrink-0 overflow-hidden">
      <div className="px-3 py-2 border-b border-white/[0.06] flex items-center justify-between gap-2 shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <Sparkles className="w-4 h-4 text-[#e7b15a] shrink-0" />
          <span className="text-[11px] font-semibold tracking-[0.12em] text-[#8a8580] uppercase truncate">
            战术简报
          </span>
        </div>
        <span className="text-[10px] font-mono text-[#2dd4bf]/90 tabular-nums shrink-0">{score.toFixed(0)}</span>
      </div>
      <div className="flex-1 min-h-0 flex flex-col gap-2.5 p-3 overflow-y-auto ti-matrix-scroll">
        <div className="flex flex-wrap gap-1.5">
          <span className="rounded-md px-2 py-0.5 text-[10px] font-medium bg-[#e7b15a]/14 text-[#e7b15a] border border-[#e7b15a]/25">
            {regimeZh}
          </span>
          <span className="rounded-md px-2 py-0.5 text-[10px] font-medium bg-white/[0.06] text-[#d4cfc7] border border-white/[0.08]">
            {modeLabel(mode)}
          </span>
        </div>
        <div>
          <div className="flex justify-between text-[9px] text-[#6b6560] mb-1">
            <span>置信</span>
            <span className="font-mono tabular-nums">{score.toFixed(0)} / 100</span>
          </div>
          <div className="h-2 rounded-full bg-black/22 overflow-hidden border border-white/[0.05]">
            <div
              className="h-full rounded-full bg-gradient-to-r from-[#b45309]/80 via-[#e7b15a] to-[#2dd4bf]/90 transition-[width] duration-300 ease-out"
              style={{ width: `${score}%` }}
            />
          </div>
        </div>
        <p className="text-[12px] leading-relaxed text-[#d4cfc7] font-sans flex-1 min-h-0">
          {reason || '等待盘面体制与研究员结论同步…'}
        </p>
      </div>
    </div>
  );
};
