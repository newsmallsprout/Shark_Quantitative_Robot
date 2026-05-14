import { useEffect, useMemo, useState } from 'react'

const PAGE_SIZE = 20

interface TradeRecord {
  symbol: string; side: string; entry_price: number; exit_price: number;
  size: number; leverage: number; margin: number; realized_pnl: number;
  gross_pnl?: number; pnl_pct: number; reason: string; fee_open: number; fee_close: number;
  opened_at: number; closed_at: number;
}

function fmtTime(ts: number) {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function TradeHistory({ trades }: { trades: TradeRecord[] }) {
  const [page, setPage] = useState(0)

  const ordered = useMemo(() => (trades?.length ? [...trades].reverse() : []), [trades])

  const total = ordered.length
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  useEffect(() => {
    setPage((p) => Math.min(p, totalPages - 1))
  }, [totalPages])

  if (!trades || !trades.length) {
    return (
      <div className="empty-state empty-state--pro">
        <div className="empty-text">暂无交易记录</div>
      </div>
    )
  }

  const safePage = Math.min(page, totalPages - 1)
  const start = safePage * PAGE_SIZE
  const pageRows = ordered.slice(start, start + PAGE_SIZE)
  const showFrom = total ? start + 1 : 0
  const showTo = Math.min(start + PAGE_SIZE, total)
  const feeTotal = (t: TradeRecord) => Number(t.fee_open || 0) + Number(t.fee_close || 0)
  const grossPnl = (t: TradeRecord) => Number.isFinite(Number(t.gross_pnl))
    ? Number(t.gross_pnl)
    : Number(t.realized_pnl || 0) + feeTotal(t)

  return (
    <div style={{ overflowX: 'auto' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: '8px',
          padding: '8px 12px',
          borderBottom: '1px solid var(--border-subtle)',
          fontSize: '11px',
          color: 'var(--text-muted)',
        }}
      >
        <span>
          第 {safePage + 1} / {totalPages} 页 · 共 {total} 笔 · 本页 {showFrom}–{showTo}
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <button
            type="button"
            className="btn btn-ghost"
            disabled={safePage <= 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            上一页
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            disabled={safePage >= totalPages - 1}
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
          >
            下一页
          </button>
        </div>
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>交易对</th>
            <th>方向</th>
            <th style={{ textAlign: 'right' }}>入场</th>
            <th style={{ textAlign: 'right' }}>出场</th>
            <th style={{ textAlign: 'right' }}>杠杆</th>
            <th style={{ textAlign: 'right' }}>毛利润</th>
            <th style={{ textAlign: 'right' }}>手续费</th>
            <th style={{ textAlign: 'right' }}>净利润</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          {pageRows.map((t, i) => (
            <tr key={`${t.symbol}-${t.closed_at}-${start + i}`}>
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
              <td style={{ textAlign: 'right' }} className={grossPnl(t) >= 0 ? 'pnl-up' : 'pnl-down'}>
                {grossPnl(t) >= 0 ? '+' : ''}{grossPnl(t).toFixed(4)}
              </td>
              <td style={{ textAlign: 'right', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                -{feeTotal(t).toFixed(4)}
              </td>
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
