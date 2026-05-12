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
      el.classList.remove('flash-down')
      void el.offsetWidth
      el.classList.add('flash-up')
    } else if (value < prev.current) {
      el.classList.remove('flash-up')
      void el.offsetWidth
      el.classList.add('flash-down')
    }
    prev.current = value
  }, [value])
  return ref
}

export default function Dashboard({
  equity,
  balance,
  freeCash,
  realizedPnl,
  winRate,
  positions,
  equityChange,
  safetyBlocked,
  totalFees,
  marginLocked,
}: Props) {
  const refEq = useFlashRef(equity)
  const refBal = useFlashRef(balance)
  const refMargin = useFlashRef(marginLocked)
  const refFree = useFlashRef(freeCash)
  const refReal = useFlashRef(realizedPnl)
  const refWin = useFlashRef(winRate)
  const refPos = useFlashRef(positions)
  const refSafe = useFlashRef(safetyBlocked ? 1 : 0)
  const refFees = useFlashRef(totalFees)

  const equityCls = equity >= 100 ? 'pnl-up' : 'pnl-down'

  return (
    <div className="kpi-strip-scroll">
      <table className="kpi-table-desk" aria-label="KPI overview">
        <thead>
          <tr>
            <th scope="col">总权益</th>
            <th scope="col">余额</th>
            <th scope="col">锁定保证金</th>
            <th scope="col">可用</th>
            <th scope="col">已实现盈亏</th>
            <th scope="col">胜率</th>
            <th scope="col">持仓数</th>
            <th scope="col">风控</th>
            <th scope="col">累计手续费</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>
              <div ref={refEq} className={`kpi-table-value ${equityCls}`}>
                ${equity.toFixed(2)}
              </div>
              <div className="kpi-table-sub" style={{ color: 'var(--text-muted)' }}>
                {equityChange >= 0 ? '+' : ''}
                {equityChange.toFixed(2)} USDT
              </div>
            </td>
            <td>
              <div ref={refBal} className="kpi-table-value pnl-neutral">
                ${balance.toFixed(2)}
              </div>
            </td>
            <td>
              <div ref={refMargin} className="kpi-table-value pnl-neutral">
                ${marginLocked.toFixed(2)}
              </div>
            </td>
            <td>
              <div ref={refFree} className={`kpi-table-value ${freeCash > 0 ? 'pnl-up' : 'pnl-down'}`}>
                ${freeCash.toFixed(2)}
              </div>
            </td>
            <td>
              <div ref={refReal} className={realizedPnl >= 0 ? 'kpi-table-value pnl-up' : 'kpi-table-value pnl-down'}>
                {realizedPnl >= 0 ? '+' : ''}
                {realizedPnl.toFixed(4)}
              </div>
            </td>
            <td>
              <div ref={refWin} className={winRate >= 0.5 ? 'kpi-table-value pnl-up' : 'kpi-table-value pnl-down'}>
                {(winRate * 100).toFixed(1)}%
              </div>
            </td>
            <td>
              <div ref={refPos} className="kpi-table-value pnl-neutral">{positions}</div>
            </td>
            <td>
              <div ref={refSafe} className={safetyBlocked ? 'kpi-table-value pnl-down' : 'kpi-table-value pnl-up'}>
                {safetyBlocked ? '熔断' : '正常'}
              </div>
            </td>
            <td>
              <div ref={refFees} className="kpi-table-value pnl-neutral">
                ${totalFees.toFixed(4)}
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  )
}
