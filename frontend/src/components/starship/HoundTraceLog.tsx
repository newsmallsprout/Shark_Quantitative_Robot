import React, { useEffect, useRef } from 'react';
import { useStore } from '../../store/useStore';
import { Terminal } from 'lucide-react';

export const HoundTraceLog: React.FC = () => {
  const houndTraces = useStore((s) => s.houndTraces);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = 0;
  }, [houndTraces.length]);

  return (
    <div className="ti-glass rounded-lg flex flex-col min-h-0 h-full overflow-hidden">
      <div className="px-3 py-2 border-b border-white/[0.06] flex items-center gap-2 shrink-0">
        <Terminal className="w-4 h-4 text-[#FFB300]" />
        <span className="text-[10px] font-semibold tracking-[0.12em] text-[#7a8499] uppercase">
          猎犬轨迹
        </span>
      </div>
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto p-2 font-mono text-[10px] leading-tight ti-matrix-scroll"
      >
        {houndTraces.length === 0 ? (
          <p className="text-[#5c6578] p-2 text-[10px]">显著 OBI 跳变时出现（已节流）</p>
        ) : (
          houndTraces.map((line, i) => {
            const pipe = line.indexOf('|');
            const head = pipe >= 0 ? line.slice(0, pipe + 1) : line.slice(0, 20);
            const tail = pipe >= 0 ? line.slice(pipe + 1).trimStart() : '';
            return (
              <div
                key={`${i}-${line.slice(0, 28)}`}
                className="py-0.5 px-1 border-b border-white/[0.03] text-[9px] leading-snug text-[#8b93a8] hover:bg-white/[0.02]"
              >
                <span className="text-[#e7b15a]/80">{head}</span>
                <span className="text-[#6a7388]">{tail}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};
