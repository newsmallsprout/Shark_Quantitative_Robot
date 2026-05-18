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
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        borderRadius: 'inherit',
        background: 'rgba(11, 14, 20, 0.75)',
        backdropFilter: 'blur(8px)',
      }}
    >
      <TradingReportChart trades={status.trade_history || []} />
    </div>
  )
}
