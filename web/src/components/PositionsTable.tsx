import type { Position } from '../store/useStore'

export default function PositionsTable({ positions }: { positions: Position[] }) {
  if (!positions.length) {
    return (
      <div className="empty-state">
        <div className="empty-icon">📊</div>
        <div className="empty-text">暂无持仓 — 等待交易信号</div>
      </div>
    )
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            <th>交易对</th>
            <th>方向</th>
            <th style={{ textAlign: 'right' }}>当前价</th>
            <th style={{ textAlign: 'right' }}>入场价</th>
            <th style={{ textAlign: 'right' }}>杠杆</th>
            <th style={{ textAlign: 'right' }}>保证金</th>
            <th style={{ textAlign: 'right' }}>盈亏%</th>
            <th style={{ textAlign: 'right' }}>未实现</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p, i) => (
            <tr key={p.symbol + i}>
              <td style={{ fontWeight: 600, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
                {p.symbol}
              </td>
              <td>
                <span className={`badge ${p.side === 'long' ? 'badge-long' : 'badge-short'}`}>
                  {p.side === 'long' ? '做多' : '做空'}
                </span>
              </td>
              <td style={{ textAlign: 'right' }}>
                ${p.current_price?.toFixed(p.symbol === 'BTC/USDT' ? 1 : 4) ?? '--'}
              </td>
              <td style={{ textAlign: 'right' }}>${p.entry_price?.toFixed(4)}</td>
              <td style={{ textAlign: 'right' }}>{p.leverage}x</td>
              <td style={{ textAlign: 'right' }}>${p.margin?.toFixed(2)}</td>
              <td style={{ textAlign: 'right' }} className={p.pnl_pct >= 0 ? 'pnl-up' : 'pnl-down'}>
                {p.pnl_pct >= 0 ? '+' : ''}{p.pnl_pct?.toFixed(1)}%
              </td>
              <td style={{ textAlign: 'right' }} className={p.unrealized_pnl >= 0 ? 'pnl-up' : 'pnl-down'}>
                {p.unrealized_pnl >= 0 ? '+' : ''}{p.unrealized_pnl?.toFixed(4)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
