import { useMemo } from 'react'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
  ReferenceLine
} from 'recharts'
import type { Position } from '../store/useStore'

interface Props {
  data: Position[]
}

export default function TradingReportChart({ data }: Props) {
  // 按照浮亏浮盈进行排序，让图表看起来更有层次感（亏的在左边，赚的在右边）
  const chartData = useMemo(() => {
    if (!data || data.length === 0) return []
    
    return [...data]
      .sort((a, b) => a.unrealized_pnl - b.unrealized_pnl)
      .map(p => ({
        name: p.symbol.split('/')[0], // 只取基础币种名称，如 BTC
        pnl: Number(p.unrealized_pnl.toFixed(2)),
        side: p.side,
        leverage: p.leverage,
        margin: p.margin
      }))
  }, [data])

  if (chartData.length === 0) {
    return (
      <div style={{
        width: '100%',
        height: '100%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: '#8899BB',
        fontFamily: 'var(--font-mono)'
      }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 24, marginBottom: 8 }}>🦈</div>
          <div>当前空仓，等待交易信号...</div>
        </div>
      </div>
    )
  }

  const CustomTooltip = ({ active, payload, label }: any) => {
    if (active && payload && payload.length) {
      const pData = payload[0].payload
      const isProfit = pData.pnl >= 0
      return (
        <div style={{
          backgroundColor: 'rgba(15, 20, 30, 0.9)',
          border: '1px solid rgba(255,255,255,0.1)',
          padding: '8px 12px',
          borderRadius: '4px',
          fontFamily: 'var(--font-mono)',
          fontSize: '12px'
        }}>
          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: '4px' }}>
            {label}/USDT ({pData.side === 'long' ? '做多' : '做空'} {pData.leverage}x)
          </div>
          <div style={{ color: isProfit ? '#00FF88' : '#FF4444' }}>
            浮动盈亏: {pData.pnl > 0 ? '+' : ''}{pData.pnl} USDT
          </div>
          <div style={{ color: '#8899BB', marginTop: '2px' }}>
            占用保证金: {pData.margin.toFixed(2)} USDT
          </div>
        </div>
      )
    }
    return null
  }

  return (
    <div style={{ width: '100%', height: '100%', padding: '20px 10px 40px 0' }}>
      <div style={{ 
        position: 'absolute', 
        top: 10, 
        left: 20, 
        color: '#8899BB',
        fontSize: 12,
        fontFamily: 'var(--font-mono)',
        fontWeight: 'bold'
      }}>
        实时持仓战报 (Real-time PnL)
      </div>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={chartData}
          margin={{
            top: 40,
            right: 20,
            left: 0,
            bottom: 5,
          }}
        >
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" vertical={false} />
          <XAxis 
            dataKey="name" 
            tick={{ fill: '#8899BB', fontSize: 10, fontFamily: 'var(--font-mono)' }}
            axisLine={{ stroke: 'rgba(255,255,255,0.1)' }}
            tickLine={false}
          />
          <YAxis 
            tick={{ fill: '#8899BB', fontSize: 10, fontFamily: 'var(--font-mono)' }}
            axisLine={false}
            tickLine={false}
            tickFormatter={(value) => `${value > 0 ? '+' : ''}${value}`}
          />
          <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255,255,255,0.02)' }} />
          <ReferenceLine y={0} stroke="rgba(255,255,255,0.2)" />
          <Bar 
            dataKey="pnl" 
            radius={[4, 4, 4, 4]}
            animationDuration={500}
          >
            {chartData.map((entry, index) => (
              <Cell 
                key={`cell-${index}`} 
                fill={entry.pnl >= 0 ? 'rgba(0, 255, 136, 0.8)' : 'rgba(255, 68, 68, 0.8)'} 
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}