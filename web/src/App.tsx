import { useEffect, useRef, useState } from 'react'
import { useStore, type Status } from './store/useStore'
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

function num(v: unknown, fallback: number): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '') {
    const x = Number(v)
    if (Number.isFinite(x)) return x
  }
  return fallback
}

function bool(v: unknown, fallback: boolean): boolean {
  if (typeof v === 'boolean') return v
  return fallback
}

/** Docker/生产同源服务用 /api/bootstrap.js 注入；本地 dev 可用 VITE_SHARK_API_TOKEN */
function dashboardApiToken(): string {
  const b = typeof window !== 'undefined' ? window.__SHARK_API_TOKEN__ : undefined
  if (typeof b === 'string' && b.trim() !== '') return b.trim()
  return import.meta.env.VITE_SHARK_API_TOKEN?.trim() ?? ''
}

function applyDashboardPayload(
  data: Record<string, unknown>,
  setStatus: (s: Partial<Status>) => void,
) {
  if (!data || typeof data !== 'object') return
  const prev = useStore.getState().status
  setStatus({
    equity: num(data.equity, prev.equity),
    balance: num(data.balance, prev.balance),
    free_cash: num(data.free_cash, prev.free_cash),
    initial_capital: num(data.initial_capital, prev.initial_capital),
    unrealized_pnl: num(data.unrealized_pnl, prev.unrealized_pnl),
    positions: num(data.positions, prev.positions),
    realized_pnl: num(data.realized_pnl, prev.realized_pnl),
    win_rate: num(data.win_rate, prev.win_rate),
    safety_blocked: bool(data.safety_blocked, prev.safety_blocked),
    position_list: Array.isArray(data.position_list) ? data.position_list : prev.position_list,
    live_prices: data.live_prices && typeof data.live_prices === 'object'
      ? (data.live_prices as Status['live_prices'])
      : prev.live_prices,
    total_fees: num(data.total_fees, prev.total_fees),
    total_slippage: num(data.total_slippage, prev.total_slippage),
    trade_history: Array.isArray(data.trade_history) ? data.trade_history : prev.trade_history,
    margin_locked: num(data.margin_locked, prev.margin_locked),
    character_event: (data.character_event as Status['character_event']) || undefined,
  })
}

/** 超过此时长未收到快照/WL 推送则视为断连（与轮询 2.5s 对齐） */
const DATA_STALE_MS = 12_000

export default function App() {
  const { status, connected, setStatus, setConnected } = useStore()
  const wsRef = useRef<WebSocket>()
  const [uptime, setUptime] = useState(0)
  const [pollLatencyMs, setPollLatencyMs] = useState<number | null>(null)
  const lastDataAtRef = useRef(0)

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

    const markDataOk = () => {
      lastDataAtRef.current = Date.now()
      setConnected(true)
    }

    const pollSnapshot = async () => {
      const t0 = performance.now()
      try {
        const tok = dashboardApiToken()
        const q = tok ? `?token=${encodeURIComponent(tok)}` : ''
        const r = await fetch(`/api/snapshot${q}`, { cache: 'no-store' })
        if (!r.ok) {
          setPollLatencyMs(null)
          return
        }
        const d = (await r.json()) as Record<string, unknown>
        applyDashboardPayload(d, setStatus)
        setPollLatencyMs(Math.round(performance.now() - t0))
        markDataOk()
      } catch {
        setPollLatencyMs(null)
      }
    }

    const staleCheck = () => {
      if (Date.now() - lastDataAtRef.current > DATA_STALE_MS)
        setConnected(false)
    }
    const staleId = setInterval(staleCheck, 1500)

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      const tok = dashboardApiToken()
      const path = tok
        ? `/ws?token=${encodeURIComponent(tok)}`
        : '/ws'
      const ws = new WebSocket(`${proto}//${location.host}${path}`)
      wsRef.current = ws
      ws.onopen = () => {
        void pollSnapshot()
      }
      ws.onclose = () => { setTimeout(connect, 2000) }
      ws.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data) as Record<string, unknown>
          // 直接同步应用（1Hz 无需 rAF；避免后台标签/部分环境下帧回调不跑导致界面卡死）
          applyDashboardPayload(d, setStatus)
          markDataOk()
        } catch (err) {
          console.warn('[shark] ws message parse failed', err)
        }
      }
    }
    connect()
    const pollId = setInterval(() => { void pollSnapshot() }, 2500)
    void pollSnapshot()
    return () => {
      clearInterval(id)
      clearInterval(staleId)
      clearInterval(pollId)
      wsRef.current?.close()
    }
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
              <SafetyPanel
                blocked={status.safety_blocked}
                connected={connected}
                latencyMs={pollLatencyMs}
                uptimeLabel={fmtUptime}
              />
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
