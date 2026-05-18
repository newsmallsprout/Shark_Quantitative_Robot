import { useStore } from '../store/useStore'
import TradingReportChart from './TradingReportChart'

export default function LoliRoom() {
  const status = useStore((s) => s.status)

  return (
    <div
      className="loli-room"
      style={{
        position: 'relative',
        width: '100%',
        flex: 1,
        minHeight: 400,
        overflow: 'hidden',
        borderRadius: 'inherit',
        background: 'var(--bg-color)',
      }}
    >
      <TradingReportChart data={status.position_list} />

      <div
        style={{
          position: 'absolute',
          bottom: 8,
          left: 8,
          right: 8,
          display: 'flex',
          flexWrap: 'wrap',
          gap: 4,
          justifyContent: 'center',
          pointerEvents: 'none',
          zIndex: 5,
        }}
      >
        {status.position_list.length === 0 ? (
          <span style={{ fontSize: 10, color: '#8899BB' }}>空仓</span>
        ) : (
          status.position_list.map((p) => {
            const up = p.unrealized_pnl >= 0
            return (
              <span
                key={p.symbol}
                style={{
                  fontSize: 9,
                  fontFamily: 'var(--font-mono)',
                  color: up ? '#00FF88' : '#FF4444',
                  background: 'rgba(0,0,0,0.5)',
                  padding: '1px 4px',
                  borderRadius: 3,
                }}
              >
                {p.symbol.split('/')[0]} {up ? '+' : ''}
                {p.unrealized_pnl.toFixed(2)}
              </span>
            )
          })
        )}
      </div>
    </div>
  )
}
