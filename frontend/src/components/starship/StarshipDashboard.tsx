import React from 'react';
import { SymbolRadar } from './SymbolRadar';
import { TacticalCenter } from './TacticalCenter';
import { StarshipPositions } from './StarshipPositions';
import { BattleReportDeck } from './BattleReportDeck';
import { StarshipCandleChart } from './StarshipCandleChart';
import KPICards from '../KPICards';

function CommandDeck() {
  return (
    <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1.1fr)_minmax(340px,0.9fr)] gap-2 min-h-0 h-full overflow-hidden">
      <div className="min-h-0 min-w-0 grid grid-rows-[auto_minmax(0,1fr)] gap-2 overflow-hidden">
        <div className="min-h-0 min-w-0 overflow-hidden">
          <TacticalCenter compact />
        </div>
        <div className="min-h-0 min-w-0 overflow-hidden">
          <StarshipPositions />
        </div>
      </div>
      <div className="min-h-0 min-w-0 overflow-hidden">
        <BattleReportDeck />
      </div>
    </div>
  );
}

export const StarshipDashboard: React.FC = () => {
  return (
    <div className="flex-1 min-h-0 w-full max-w-[1920px] mx-auto p-2 md:p-2.5 box-border flex flex-col overflow-hidden">
      <div
        className="flex-1 min-h-0 grid text-slate-800 overflow-hidden"
        style={{
          gridTemplateAreas: `
          "head head"
          "chart side"
          "deck side"`,
          gridTemplateColumns: 'minmax(0, 1.35fr) minmax(220px, 280px)',
          gridTemplateRows: 'auto minmax(280px, 42vh) minmax(0, 1fr)',
          gap: '0.625rem',
        }}
      >
        <div style={{ gridArea: 'head' }} className="ti-glass rounded-lg px-3 py-2 min-h-0">
          <KPICards />
        </div>

        <div style={{ gridArea: 'chart' }} className="min-h-0 min-w-0 overflow-hidden shrink-0">
          <StarshipCandleChart />
        </div>

        <div style={{ gridArea: 'side' }} className="min-h-0 min-w-0 overflow-hidden">
          <SymbolRadar />
        </div>

        <div style={{ gridArea: 'deck' }} className="min-h-0 min-w-0 overflow-hidden">
          <CommandDeck />
        </div>
      </div>
    </div>
  );
};
