import type { Position } from '../store/useStore'

interface TradeRecord {
  symbol: string; side: string; entry_price: number; exit_price: number;
  size: number; leverage: number; margin: number; realized_pnl: number;
  pnl_pct: number; reason: string; fee_open: number; fee_close: number;
  opened_at: number; closed_at: number;
}

function fmtTime(ts: number) {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function TradeHistory({ trades }: { trades: TradeRecord[] }) {
  if (!trades || !trades.length) {
    return (
      <div className="empty-state">
        <div className="empty-icon">📋</div>
        <div className="empty-text">暂无交易记录</div>
      </div>
    )
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>交易对</th>
            <th>方向</th>
            <th style={{ textAlign: 'right' }}>入场</th>
            <th style={{ textAlign: 'right' }}>出场</th>
            <th style={{ textAlign: 'right' }}>杠杆</th>
            <th style={{ textAlign: 'right' }}>盈亏</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          {[...trades].reverse().map((t, i) => (
            <tr key={i}>
              <td style={{ color: 'var(--text-muted)', fontSize: '11px' }}>{fmtTime(t.closed_at)}</td>
              <td style={{ fontWeight: 600, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
                {t.symbol}
              </td>
              <td>
                <span className={`badge ${t.side === 'long' ? 'badge-long' : 'badge-short'}`}>
                  {t.side === 'long' ? '做多' : '做空'}
                </span>
              </td>
              <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>
                ${t.entry_price?.toFixed(4)}
              </td>
              <td style={{ textAlign: 'right', fontFamily: 'var(--font-mono)' }}>
                ${t.exit_price?.toFixed(4)}
              </td>
              <td style={{ textAlign: 'right' }}>{t.leverage}x</td>
              <td style={{ textAlign: 'right' }} className={t.realized_pnl >= 0 ? 'pnl-up' : 'pnl-down'}>
                {t.realized_pnl >= 0 ? '+' : ''}{t.realized_pnl?.toFixed(4)}
              </td>
              <td style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{t.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
