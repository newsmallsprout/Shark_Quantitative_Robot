import { useEffect, useRef } from 'react'

interface Props {
  equity: number; balance: number; freeCash: number; realizedPnl: number;
  winRate: number; positions: number; equityChange: number;
  safetyBlocked: boolean; totalFees: number; marginLocked: number;
}

function useFlashRef(value: number) {
  const ref = useRef<HTMLDivElement>(null)
  const prev = useRef(value)
  useEffect(() => {
    if (!ref.current) return
    const el = ref.current
    if (value > prev.current) {
      el.classList.remove('flash-up'); void el.offsetWidth; el.classList.add('flash-up')
    } else if (value < prev.current) {
      el.classList.remove('flash-down'); void el.offsetWidth; el.classList.add('flash-down')
    }
    prev.current = value
  }, [value])
  return ref
}

function KpiCard({ label, value, sub, cls }: { label: string; value: string; sub?: string; cls: string }) {
  const flashRef = useFlashRef(parseFloat(value.replace(/[^0-9.-]/g, '')) || 0)
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div ref={flashRef} className={`kpi-value ${cls}`}>{value}</div>
      {sub && <div className="kpi-sub" style={{ color: 'var(--text-muted)' }}>{sub}</div>}
    </div>
  )
}

export default function Dashboard({ equity, balance, freeCash, realizedPnl, winRate, positions, equityChange, safetyBlocked, totalFees, marginLocked }: Props) {
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
      gap: '8px',
    }}>
      <KpiCard
        label="总权益"
        value={`$${equity.toFixed(2)}`}
        sub={`${equityChange >= 0 ? '+' : ''}${equityChange.toFixed(2)} USDT`}
        cls={equity >= 100 ? 'pnl-up' : 'pnl-down'}
      />
      <KpiCard
        label="余额"
        value={`$${balance.toFixed(2)}`}
        cls="pnl-neutral"
      />
      <KpiCard
        label="锁定保证金"
        value={`$${marginLocked.toFixed(2)}`}
        cls="pnl-neutral"
      />
      <KpiCard
        label="可用"
        value={`$${freeCash.toFixed(2)}`}
        cls={freeCash > 0 ? 'pnl-up' : 'pnl-down'}
      />
      <KpiCard
        label="已实现盈亏"
        value={`${realizedPnl >= 0 ? '+' : ''}${realizedPnl.toFixed(4)}`}
        cls={realizedPnl >= 0 ? 'pnl-up' : 'pnl-down'}
      />
      <KpiCard
        label="胜率"
        value={`${(winRate * 100).toFixed(1)}%`}
        cls={winRate >= 0.5 ? 'pnl-up' : 'pnl-down'}
      />
      <KpiCard
        label="持仓数"
        value={`${positions}`}
        cls="pnl-neutral"
      />
      <KpiCard
        label="风控"
        value={safetyBlocked ? '熔断' : '正常'}
        cls={safetyBlocked ? 'pnl-down' : 'pnl-up'}
      />
      <KpiCard
        label="累计手续费"
        value={`$${totalFees.toFixed(4)}`}
        cls="pnl-neutral"
      />
    </div>
  )
}
