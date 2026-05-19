import React, { useMemo, useState } from 'react'
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

interface TradeRecord {
  symbol: string;
  side: string;
  entry_price: number;
  exit_price: number;
  size: number;
  leverage: number;
  margin: number;
  realized_pnl: number;
  gross_pnl?: number;
  pnl_pct: number;
  reason: string;
  fee_open: number;
  fee_close: number;
  opened_at: number;
  closed_at: number;
}

interface Props {
  trades: TradeRecord[]
}

type TimeFrame = 'hour' | 'day' | 'week'

export default function TradingReportChart({ trades }: Props) {
  const [timeframe, setTimeframe] = useState<TimeFrame>('hour')

  const chartData = useMemo(() => {
    if (!trades || trades.length === 0) return []

    // 分组逻辑
    const grouped = new Map<string, { timeLabel: string; pnl: number; tradesCount: number }>()

    trades.forEach(t => {
      // 如果后端没有返回 closed_at，则使用 opened_at 作为降级（对于平仓记录通常至少有 opened_at）
      const timestamp = t.closed_at || t.opened_at
      if (!timestamp) return
      
      const date = new Date(timestamp * 1000)
      let key = ''
      let label = ''

      if (timeframe === 'hour') {
        // 按小时分组: YYYY-MM-DD HH:00
        key = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}-${date.getHours()}`
        label = `${date.getMonth() + 1}/${date.getDate()} ${date.getHours().toString().padStart(2, '0')}:00`
      } else if (timeframe === 'day') {
        // 按天分组
        key = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`
        label = `${date.getMonth() + 1}/${date.getDate()}`
      } else if (timeframe === 'week') {
        // 按周分组 (简单处理：以周一为起点)
        const day = date.getDay()
        const diff = date.getDate() - day + (day === 0 ? -6 : 1) // 调整到周一
        const monday = new Date(date.setDate(diff))
        key = `${monday.getFullYear()}-${monday.getMonth()}-${monday.getDate()}`
        label = `${monday.getMonth() + 1}/${monday.getDate()} 周`
      }

      const existing = grouped.get(key) || { timeLabel: label, pnl: 0, tradesCount: 0 }
      existing.pnl += (t.realized_pnl || 0)
      existing.tradesCount += 1
      grouped.set(key, existing)
    })

    // 转为数组并按时间排序 (Map遍历顺序即插入顺序，所以需要额外排序)
    // 但为了排序方便，我们可以直接把 key 转成时间戳再排
    const result = Array.from(grouped.entries()).map(([k, v]) => {
      return {
        ...v,
        pnl: Number(v.pnl.toFixed(2)),
        sortKey: k
      }
    })

    // 根据 timeframe 决定排序的逻辑，保证时间是正序的（左边旧，右边新）
    result.sort((a, b) => {
      const partsA = a.sortKey.split('-').map(Number)
      const partsB = b.sortKey.split('-').map(Number)
      for (let i = 0; i < partsA.length; i++) {
        if (partsA[i] !== partsB[i]) return partsA[i] - partsB[i]
      }
      return 0
    })

    return result
  }, [trades, timeframe])

  if (!chartData || chartData.length === 0) {
    return (
      <div style={{ width: '100%', height: '100%', padding: '20px 20px 40px 10px', display: 'flex', flexDirection: 'column' }}>
        <div style={{ 
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          paddingLeft: '10px',
          marginBottom: '20px'
        }}>
          <div style={{ 
            color: '#E0E6F0',
            fontSize: 15,
            fontFamily: 'var(--font-mono)',
            fontWeight: 'bold',
            letterSpacing: '1px'
          }}>
            历史盈亏战报 (Historical PnL)
          </div>
          <div style={{ display: 'flex', gap: '8px' }}>
            {(['hour', 'day', 'week'] as TimeFrame[]).map(tf => (
              <button
                key={tf}
                onClick={() => setTimeframe(tf)}
                style={{
                  background: timeframe === tf ? 'rgba(0, 255, 136, 0.15)' : 'rgba(255, 255, 255, 0.05)',
                  color: timeframe === tf ? '#00FF88' : '#8899BB',
                  border: `1px solid ${timeframe === tf ? 'rgba(0, 255, 136, 0.3)' : 'transparent'}`,
                  padding: '4px 12px',
                  borderRadius: '4px',
                  cursor: 'pointer',
                  fontSize: '12px',
                  fontFamily: 'var(--font-mono)',
                  transition: 'all 0.2s'
                }}
              >
                {tf === 'hour' ? '小时' : tf === 'day' ? '日报' : '周报'}
              </button>
            ))}
          </div>
        </div>
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center',
          justifyContent: 'center', color: '#8899BB', fontFamily: 'var(--font-mono)'
        }}>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 32, marginBottom: 12, opacity: 0.8 }}>📊</div>
            <div style={{ letterSpacing: '1px' }}>暂无有效历史交易数据，等待产出战报...</div>
          </div>
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
          padding: '10px 14px',
          borderRadius: '6px',
          fontFamily: 'var(--font-mono)',
          fontSize: '12px'
        }}>
          <div style={{ color: '#fff', fontWeight: 'bold', marginBottom: '6px', borderBottom: '1px solid rgba(255,255,255,0.1)', paddingBottom: '4px' }}>
            {pData.timeLabel}
          </div>
          <div style={{ color: isProfit ? '#00FF88' : '#FF4444', fontSize: '14px', fontWeight: 'bold' }}>
            净利润: {pData.pnl > 0 ? '+' : ''}{pData.pnl} USDT
          </div>
          <div style={{ color: '#8899BB', marginTop: '4px' }}>
            成交笔数: {pData.tradesCount} 笔
          </div>
        </div>
      )
    }
    return null
  }

  return (
    <div style={{ width: '100%', height: '100%', padding: '20px 20px 40px 10px', display: 'flex', flexDirection: 'column' }}>
      <div style={{ 
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        paddingLeft: '10px',
        marginBottom: '20px'
      }}>
        <div style={{ 
          color: '#E0E6F0',
          fontSize: 15,
          fontFamily: 'var(--font-mono)',
          fontWeight: 'bold',
          letterSpacing: '1px'
        }}>
          历史盈亏战报 (Historical PnL)
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          {(['hour', 'day', 'week'] as TimeFrame[]).map(tf => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              style={{
                background: timeframe === tf ? 'rgba(0, 255, 136, 0.15)' : 'rgba(255, 255, 255, 0.05)',
                color: timeframe === tf ? '#00FF88' : '#8899BB',
                border: `1px solid ${timeframe === tf ? 'rgba(0, 255, 136, 0.3)' : 'transparent'}`,
                padding: '4px 12px',
                borderRadius: '4px',
                cursor: 'pointer',
                fontSize: '12px',
                fontFamily: 'var(--font-mono)',
                transition: 'all 0.2s'
              }}
            >
              {tf === 'hour' ? '小时' : tf === 'day' ? '日报' : '周报'}
            </button>
          ))}
        </div>
      </div>
      
      <div style={{ flex: 1, minHeight: 0 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={chartData}
            margin={{ top: 10, right: 10, left: 0, bottom: 5 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" vertical={false} />
            <XAxis 
              dataKey="timeLabel" 
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
              maxBarSize={40}
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
    </div>
  )
}