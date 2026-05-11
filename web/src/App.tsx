import { useEffect, useRef, useState } from 'react'
import { useStore } from './store/useStore'
import Dashboard from './components/Dashboard'
import PositionsTable from './components/PositionsTable'
import TradeHistory from './components/TradeHistory'
import SafetyPanel from './components/SafetyPanel'
import LoliRoom from './components/LoliRoom'

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
  const pendingRef = useRef<any>(null)  // rAF节流缓冲区
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
          // rAF节流：累积到下一帧统一更新，防止高频WebSocket导致连锁re-render
          if (!pendingRef.current) {
            pendingRef.current = d
            requestAnimationFrame(() => {
              const data = pendingRef.current
              pendingRef.current = null
              if (!data) return
              const prev = useStore.getState().status
              setStatus({
                equity: typeof data.equity === 'number' ? data.equity : prev.equity,
                balance: typeof data.balance === 'number' ? data.balance : prev.balance,
                free_cash: typeof data.free_cash === 'number' ? data.free_cash : prev.free_cash,
                initial_capital: typeof data.initial_capital === 'number' ? data.initial_capital : prev.initial_capital,
                unrealized_pnl: typeof data.unrealized_pnl === 'number' ? data.unrealized_pnl : prev.unrealized_pnl,
                positions: typeof data.positions === 'number' ? data.positions : prev.positions,
                realized_pnl: typeof data.realized_pnl === 'number' ? data.realized_pnl : prev.realized_pnl,
                win_rate: typeof data.win_rate === 'number' ? data.win_rate : prev.win_rate,
                safety_blocked: typeof data.safety_blocked === 'boolean' ? data.safety_blocked : prev.safety_blocked,
                position_list: Array.isArray(data.position_list) ? data.position_list : prev.position_list,
                live_prices: data.live_prices && typeof data.live_prices === 'object' ? data.live_prices : prev.live_prices,
                total_fees: typeof data.total_fees === 'number' ? data.total_fees : prev.total_fees,
                total_slippage: typeof data.total_slippage === 'number' ? data.total_slippage : prev.total_slippage,
                trade_history: Array.isArray(data.trade_history) ? data.trade_history : prev.trade_history,
                margin_locked: typeof data.margin_locked === 'number' ? data.margin_locked : prev.margin_locked,
                character_event: data.character_event || undefined,
              })
            })
          } else {
            pendingRef.current = d  // 覆盖为最新数据
          }
        } catch {}
      }
    }
    connect()
    return () => { clearInterval(id); wsRef.current?.close() }
  }, [])

  const equityChange = status.equity - status.initial_capital
  const fmtUptime = `${String(Math.floor(uptime / 3600)).padStart(2, '0')}:${String(Math.floor((uptime % 3600) / 60)).padStart(2, '0')}:${String(uptime % 60).padStart(2, '0')}`

  return (
    <>
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* 顶栏 */}
      <div className="topbar">
        <div className="topbar-brand">
          <span className="accent">🦈 Shark 2.0</span>
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
          freeCash={status.free_cash}
          realizedPnl={status.realized_pnl}
          winRate={status.win_rate}
          positions={status.positions}
          equityChange={equityChange}
          safetyBlocked={status.safety_blocked}
          totalFees={status.total_fees}
          marginLocked={status.margin_locked}
        />

        {/* 宠物舱 + 风控 */}
        <div style={{
          display: 'grid', gridTemplateColumns: '2fr 1fr', gap: '10px', marginTop: '10px',
        }}>
          <div className="card">
            <div className="card-header">shark 领域</div>
            <div
              className="card-body"
              style={{
                padding: '8px 0 0',
                minHeight: 400,
                position: 'relative',
                display: 'flex',
                flexDirection: 'column',
                flex: 1,
              }}
            >
              <LoliRoom />
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

    </>
  )
}
