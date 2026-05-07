import { useEffect, useRef, useState, lazy, Suspense } from 'react'
import { useStore } from './store/useStore'
import Dashboard from './components/Dashboard'
import PositionsTable from './components/PositionsTable'
import TradeHistory from './components/TradeHistory'
import SafetyPanel from './components/SafetyPanel'

const ChartPanel = lazy(() => import('./components/ChartPanel'))

function Clock() {
  const [time, setTime] = useState(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
  useEffect(() => {
    const id = setInterval(() => setTime(new Date().toLocaleTimeString('zh-CN', { hour12: false })), 1000)
    return () => clearInterval(id)
  }, [])
  return <span style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--text-muted)' }}>{time}</span>
}

export default function App() {
  const { status, connected, setStatus, setConnected } = useStore()
  const wsRef = useRef<WebSocket>()
  const [uptime, setUptime] = useState(0)

  // ═══ Starfield animation ═══
  useEffect(() => {
    const canvas = document.getElementById('starfield') as HTMLCanvasElement
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let animId: number
    const stars: Array<{x:number;y:number;r:number;v:number;o:number;c:string}> = []

    function resize() {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
    }
    resize()
    window.addEventListener('resize', resize)

    // Create stars
    for (let i = 0; i < 120; i++) {
      stars.push({
        x: Math.random() * canvas.width,
        y: Math.random() * canvas.height,
        r: Math.random() * 1.5 + 0.3,
        v: Math.random() * 0.3 + 0.05,
        o: Math.random() * 0.7 + 0.3,
        c: Math.random() > 0.85 ? '#00F0FF' : Math.random() > 0.7 ? '#B026FF' : '#8899BB',
      })
    }

    function draw() {
      ctx!.clearRect(0, 0, canvas.width, canvas.height)
      for (const s of stars) {
        s.y -= s.v
        if (s.y < 0) { s.y = canvas.height; s.x = Math.random() * canvas.width }
        s.o = 0.3 + Math.sin(Date.now() * 0.002 + s.x) * 0.3
        ctx!.beginPath()
        ctx!.arc(s.x, s.y, s.r, 0, Math.PI * 2)
        ctx!.fillStyle = s.c
        ctx!.globalAlpha = Math.max(0.1, s.o)
        ctx!.fill()
      }
      ctx!.globalAlpha = 1
      animId = requestAnimationFrame(draw)
    }
    draw()

    return () => { cancelAnimationFrame(animId); window.removeEventListener('resize', resize) }
  }, [])

  useEffect(() => {
    const start = Date.now()
    const id = setInterval(() => setUptime(Math.floor((Date.now() - start) / 1000)), 1000)

    const connect = () => {
      const ws = new WebSocket(`ws://${location.host}/ws`)
      wsRef.current = ws
      ws.onopen = () => setConnected(true)
      ws.onclose = () => { setConnected(false); setTimeout(connect, 2000) }
      ws.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data)
          setStatus({
            equity: d.equity ?? status.equity,
            balance: d.balance ?? status.balance,
            positions: d.positions ?? 0,
            realized_pnl: d.realized_pnl ?? 0,
            win_rate: d.win_rate ?? 0,
            safety_blocked: d.safety_blocked ?? false,
            position_list: d.position_list || [],
            live_prices: d.live_prices || {},
            total_fees: d.total_fees ?? 0,
            total_slippage: d.total_slippage ?? 0,
            trade_history: d.trade_history || [],
            margin_locked: d.margin_locked ?? 0,
          })
        } catch {}
      }
    }
    connect()
    return () => { clearInterval(id); wsRef.current?.close() }
  }, [])

  const equityChange = status.equity - 100
  const fmtUptime = `${String(Math.floor(uptime / 3600)).padStart(2, '0')}:${String(Math.floor((uptime % 3600) / 60)).padStart(2, '0')}:${String(uptime % 60).padStart(2, '0')}`

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* 顶栏 */}
      <div className="topbar">
        <div className="topbar-brand">
          <span className="accent">🦈 Shark</span>
          <span style={{ color: 'var(--text-secondary)', marginLeft: '6px', fontSize: '13px', fontWeight: 500 }}>2.0</span>
        </div>
        <div className="topbar-right">
          <span className="badge badge-mode">模拟盘</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <span className={`status-dot ${status.safety_blocked ? 'blocked' : connected ? 'live' : 'disconnected'}`} />
            <span style={{ fontSize: '11px', color: 'var(--text-secondary)', fontWeight: 500 }}>
              {status.safety_blocked ? '熔断' : connected ? '运行中' : '离线'}
            </span>
          </div>
          <Clock />
        </div>
      </div>

      {/* 主内容 */}
      <div style={{ flex: 1, padding: '16px 20px', maxWidth: '1440px', margin: '0 auto', width: '100%' }}>
        {/* KPI 面板 */}
        <Dashboard
          equity={status.equity}
          balance={status.balance}
          realizedPnl={status.realized_pnl}
          winRate={status.win_rate}
          positions={status.positions}
          equityChange={equityChange}
          safetyBlocked={status.safety_blocked}
          totalFees={status.total_fees}
          marginLocked={status.margin_locked}
        />

        {/* 图表 + 安全 */}
        <div style={{
          display: 'grid', gridTemplateColumns: '2fr 1fr', gap: '10px', marginTop: '10px',
        }}>
          <div className="card">
            <div className="card-header">行情概览</div>
            <div className="card-body" style={{ padding: '8px 0 0' }}>
              <Suspense fallback={<div style={{ height: '510px', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)', fontSize: '13px' }}>加载行情图表...</div>}>
                <ChartPanel />
              </Suspense>
            </div>
          </div>
          <div className="card" style={{ display: 'flex', flexDirection: 'column' }}>
            <div className="card-header">风控状态</div>
            <div className="card-body" style={{ flex: 1 }}>
              <SafetyPanel blocked={status.safety_blocked} connected={connected} />
            </div>
            <div style={{
              padding: '8px 16px', borderTop: '1px solid var(--border-subtle)',
              fontSize: '10px', color: 'var(--text-muted)',
              display: 'flex', justifyContent: 'space-between',
            }}>
              <span>运行时长</span>
              <span style={{ fontFamily: 'var(--font-mono)' }}>{fmtUptime}</span>
            </div>
          </div>
        </div>

        {/* 持仓 */}
        <div className="card" style={{ marginTop: '10px' }}>
          <div className="card-header">
            <span>当前持仓 ({status.position_list?.length || 0})</span>
          </div>
          <div className="card-body" style={{ padding: '0' }}>
            <PositionsTable positions={status.position_list || []} />
          </div>
        </div>

        {/* 交易历史 */}
        <div className="card" style={{ marginTop: '10px' }}>
          <div className="card-header">
            <span>交易记录 ({status.trade_history?.length || 0})</span>
          </div>
          <div className="card-body" style={{ padding: '0' }}>
            <TradeHistory trades={status.trade_history || []} />
          </div>
        </div>
      </div>

      {/* 底栏 */}
      <div style={{
        padding: '10px 20px', borderTop: '1px solid var(--border-subtle)',
        fontSize: '10px', color: 'var(--text-muted)',
        display: 'flex', justifyContent: 'space-between',
        background: 'var(--bg-surface)',
      }}>
        <span>Shark 2.0 · AI 多策略量化机器人</span>
        <span style={{ fontFamily: 'var(--font-mono)' }}>Gate.io 合约 · 模拟交易</span>
      </div>
    </div>
  )
}
