import { useEffect, useRef, useState } from 'react'
import { useStore, type LiveStatus, type Status } from './store/useStore'
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

type AppView = 'dashboard' | 'support' | 'risk'

function hashToView(hash: string): AppView {
  if (hash === '#/support') return 'support'
  if (hash === '#/risk') return 'risk'
  return 'dashboard'
}

export function TopbarSectionLinks({ currentView }: { currentView: AppView }) {
  if (currentView === 'support') {
    return (
      <nav className="topbar-section-links" aria-label="页面与段落跳转">
        <a href="#">总览</a>
        <a href="#/risk">风控页</a>
        <a className="topbar-section-links__plans" href="/plans">Plans</a>
      </nav>
    )
  }
  if (currentView === 'risk') {
    return (
      <nav className="topbar-section-links" aria-label="页面与段落跳转">
        <a href="#">总览</a>
        <a href="#/support">支持页</a>
        <a className="topbar-section-links__plans" href="/plans">Plans</a>
      </nav>
    )
  }
  return (
    <nav className="topbar-section-links" aria-label="页面与段落跳转">
      <a href="#section-kpi">KPI</a>
      <a href="#section-room">舱室</a>
      <a href="#section-positions">持仓</a>
      <a href="#section-history">历史</a>
      <a href="#/risk">风控页</a>
      <a href="#/support">支持页</a>
      <a className="topbar-section-links__plans" href="/plans">Plans</a>
    </nav>
  )
}

function SupportPage() {
  return (
    <div className="subpage-layout">
      <div className="subpage-header">
        <div>
          <div className="subpage-title">支持 Shark</div>
          <div className="subpage-subtitle">收款码已从 dashboard 拆出，避免遮挡主视图。</div>
        </div>
        <a className="subpage-backlink" href="#">
          返回总览
        </a>
      </div>
      <div className="support-grid">
        <div className="card support-card">
          <div className="card-header">微信支付</div>
          <div className="card-body">
            <div className="support-qr-frame">
              <img src="/static/IMG_2372.jpg" alt="微信收款码" />
            </div>
          </div>
        </div>
        <div className="card support-card">
          <div className="card-header">支付宝</div>
          <div className="card-body">
            <div className="support-qr-frame">
              <img src="/static/IMG_2373.jpg" alt="支付宝收款码" />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function RiskPage({
  status,
  riskHalt,
  connected,
  pollLatencyMs,
  fmtUptime,
}: {
  status: Status
  riskHalt: boolean
  connected: boolean
  pollLatencyMs: number | null
  fmtUptime: string
}) {
  return (
    <div className="subpage-layout">
      <div className="subpage-header">
        <div>
          <div className="subpage-title">风控与计划状态</div>
          <div className="subpage-subtitle">计划状态、Plan API 异常和风控闸门已从 dashboard 拆出单独查看。</div>
        </div>
        <a className="subpage-backlink" href="#">
          返回总览
        </a>
      </div>
      <div className="card">
        <div className="card-header">运行面板</div>
        <div className="card-body">
          <SafetyPanel
            status={status}
            blocked={riskHalt}
            connected={connected}
            latencyMs={pollLatencyMs}
            uptimeLabel={fmtUptime}
          />
        </div>
      </div>
    </div>
  )
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

type ConfirmPayload = {
  title: string
  message: string
  confirmLabel?: string
  /** danger = 红渐变（同「开始交易」）；safe = 绿描边（同「停止交易」） */
  confirmStyle?: 'danger' | 'safe'
  onConfirm: () => boolean | void | Promise<boolean | void>
}

export function ConfirmModal({
  open,
  title,
  message,
  confirmLabel = '确定',
  confirmStyle = 'danger',
  onConfirm,
  onCancel,
}: ConfirmPayload & { open: boolean; onCancel: () => void }) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onCancel])

  if (!open) return null

  return (
    <div
      className="shark-confirm-overlay"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel()
      }}
    >
      <div
        className="shark-confirm-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="shark-confirm-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div id="shark-confirm-title" className="shark-confirm-title">
          {title}
        </div>
        <div className="shark-confirm-body">{message}</div>
        <div className="shark-confirm-actions">
          <button type="button" className="btn-modal-cancel" onClick={onCancel}>
            取消
          </button>
          <button
            type="button"
            className={
              confirmStyle === 'safe'
                ? 'btn-live-trade btn-live-trade--stop'
                : 'btn-live-trade btn-live-trade--start'
            }
            onClick={async () => {
              try {
                const r = onConfirm()
                const result = r instanceof Promise ? await r : r
                if (result === false) return
                onCancel()
              } catch {
                // 出错保持弹窗不关闭
              }
            }}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

/** Docker/生产同源服务用 /api/bootstrap.js 注入；本地 dev 可用 VITE_SHARK_API_TOKEN */
function dashboardApiToken(): string {
  const b = typeof window !== 'undefined' ? window.__SHARK_API_TOKEN__ : undefined
  if (typeof b === 'string' && b.trim() !== '') return b.trim()
  return import.meta.env.VITE_SHARK_API_TOKEN?.trim() ?? ''
}

function dashboardLicense(): string {
  try {
    return localStorage.getItem('shark_license') || ''
  } catch {
    return ''
  }
}

function dashboardAuthHeaders(init?: HeadersInit): Headers {
  const h = new Headers(init)
  const tok = dashboardApiToken()
  if (tok) h.set('Authorization', `Bearer ${tok}`)
  const lic = dashboardLicense()
  if (lic) h.set('X-Shark-License', lic)
  return h
}

async function showImageDenyIfNeeded(r: Response): Promise<boolean> {
  const ct = r.headers.get('content-type') || ''
  if (r.status !== 403 || !ct.includes('image/')) return false
  const blob = await r.blob()
  const url = URL.createObjectURL(blob)
  document.body.innerHTML = `<div style="background:#000;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0">
    <img src="${url}" style="max-width:100vw;max-height:80vh" />
    <p style="color:#f44;font-size:16px;margin-top:16px;font-family:sans-serif">🐱 无权限 — 请配置许可证</p>
    <p style="color:#888;font-size:12px;margin-top:8px;font-family:monospace">localStorage.setItem('shark_license', '&lt;你的许可证&gt;')</p>
  </div>`
  document.body.style.margin = '0'
  return true
}

async function postLiveToggle(): Promise<{ trading_enabled?: boolean; error?: string }> {
  const r = await fetch('/api/live/toggle', {
    method: 'POST',
    headers: dashboardAuthHeaders({ 'Content-Type': 'application/json' }),
  })
  if (await showImageDenyIfNeeded(r)) return { error: '无权限' }
  let j: { trading_enabled?: boolean; error?: string } = {}
  try {
    j = (await r.json()) as { trading_enabled?: boolean; error?: string }
  } catch {
    j = {}
  }
  if (!r.ok && !j.error) {
    j.error = r.status === 401 ? '未授权：请配置与后端一致的 SHARK_API_TOKEN' : `请求失败 (${r.status})`
  }
  return j
}

async function postPaperToggle(): Promise<{ trading_enabled?: boolean; error?: string }> {
  const r = await fetch('/api/paper/toggle', {
    method: 'POST',
    headers: dashboardAuthHeaders({ 'Content-Type': 'application/json' }),
  })
  if (await showImageDenyIfNeeded(r)) return { error: '无权限' }
  let j: { trading_enabled?: boolean; error?: string } = {}
  try {
    j = (await r.json()) as { trading_enabled?: boolean; error?: string }
  } catch {
    j = {}
  }
  if (!r.ok && !j.error) {
    j.error = r.status === 401 ? '未授权：请配置与后端一致的 SHARK_API_TOKEN' : `请求失败 (${r.status})`
  }
  return j
}

async function postPaperReset(capital: number): Promise<{ ok?: boolean; error?: string }> {
  const r = await fetch('/api/paper/reset', {
    method: 'POST',
    headers: dashboardAuthHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ capital }),
  })
  if (await showImageDenyIfNeeded(r)) return { error: '无权限' }
  const j = await r.json() as { ok?: boolean; error?: string }
  return j
}

async function postLicenseLogin(license: string): Promise<{ ok?: boolean; reason?: string }> {
  const r = await fetch('/api/license/login', {
    method: 'POST',
    headers: dashboardAuthHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ license }),
  })
  return (await r.json()) as { ok?: boolean; reason?: string }
}

async function postEvoApprove(id: number): Promise<{ ok?: boolean; error?: string }> {
  const r = await fetch(`/api/evo/approve/${id}`, {
    method: 'POST',
    headers: dashboardAuthHeaders({ 'Content-Type': 'application/json' }),
  })
  if (await showImageDenyIfNeeded(r)) return { error: '无权限' }
  const j = await r.json() as { ok?: boolean; error?: string }
  return j
}

async function postEvoReject(id: number): Promise<{ ok?: boolean; error?: string }> {
  const r = await fetch(`/api/evo/reject/${id}`, {
    method: 'POST',
    headers: dashboardAuthHeaders({ 'Content-Type': 'application/json' }),
  })
  if (await showImageDenyIfNeeded(r)) return { error: '无权限' }
  const j = await r.json() as { ok?: boolean; error?: string }
  return j
}

async function postSharkMode(mode: 'paper' | 'live'): Promise<{ error?: string }> {
  const r = await fetch('/api/shark/mode', {
    method: 'POST',
    headers: dashboardAuthHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ mode }),
  })
  if (await showImageDenyIfNeeded(r)) return { error: '无权限' }
  let raw: { detail?: unknown; error?: unknown } = {}
  try {
    raw = (await r.json()) as { detail?: unknown; error?: unknown }
  } catch {
    raw = {}
  }
  const detail = typeof raw.detail === 'string' ? raw.detail : undefined
  if (!r.ok) {
    return { error: detail || (typeof raw.error === 'string' ? raw.error : undefined) || `请求失败 (${r.status})` }
  }
  return {}
}

export function normalizeDashboardPayload(
  data: Record<string, unknown>,
  prev: Status,
): Partial<Status> {
  const paperTrading = data.paper_trading
  const liveTrading = data.live_trading
  const livePayload =
    data.live !== undefined && data.live !== null && typeof data.live === 'object'
      ? (data.live as LiveStatus)
      : undefined
  const paperPayload =
    data.paper !== undefined && data.paper !== null && typeof data.paper === 'object'
      ? (data.paper as Status['paper'])
      : undefined
  return {
    equity: num(data.equity, prev.equity),
    balance: num(data.balance, prev.balance),
    free_cash: num(data.free_cash, prev.free_cash),
    initial_capital: num(data.initial_capital, prev.initial_capital),
    unrealized_pnl: num(data.unrealized_pnl, prev.unrealized_pnl),
    positions: num(data.positions, prev.positions),
    realized_pnl: num(data.realized_pnl, prev.realized_pnl),
    win_rate: num(data.win_rate, prev.win_rate),
    safety_blocked: bool(data.safety_blocked, prev.safety_blocked),
    fuse_reason: typeof data.fuse_reason === 'string' ? data.fuse_reason : prev.fuse_reason ?? '',
    live_api_ok:
      typeof data.live_api_ok === 'boolean' ? data.live_api_ok : prev.live_api_ok ?? true,
    last_tick_block: (() => {
      if (data.last_tick_block === null) return null
      if (data.last_tick_block && typeof data.last_tick_block === 'object') {
        const b = data.last_tick_block as Record<string, unknown>
        const code = typeof b.code === 'string' ? b.code : ''
        const detail = typeof b.detail === 'string' ? b.detail : ''
        const ts = typeof b.ts === 'number' && Number.isFinite(b.ts) ? b.ts : undefined
        return { code, detail, ts }
      }
      return prev.last_tick_block ?? null
    })(),
    position_list: Array.isArray(data.position_list) ? data.position_list : prev.position_list,
    live_prices: data.live_prices && typeof data.live_prices === 'object'
      ? (data.live_prices as Status['live_prices'])
      : prev.live_prices,
    total_fees: num(data.total_fees, prev.total_fees),
    total_slippage: num(data.total_slippage, prev.total_slippage),
    trade_history: Array.isArray(data.trade_history) ? data.trade_history : prev.trade_history,
    margin_locked: num(data.margin_locked, prev.margin_locked),
    character_event: (data.character_event as Status['character_event']) || undefined,
    live: livePayload
      ? {
          ...livePayload,
          trading_enabled:
            typeof liveTrading === 'boolean' ? liveTrading : livePayload.trading_enabled,
        }
      : undefined,
    paper: paperPayload ?? {
      active: true,
      trading_enabled:
        typeof paperTrading === 'boolean'
          ? paperTrading
          : prev.paper?.trading_enabled === true,
    },
    shark_mode: (() => {
      const m = data.shark_mode
      if (m === 'live' || m === 'paper') return m
      return prev.shark_mode ?? 'paper'
    })(),
    evo_pending: Array.isArray(data.evo_pending) ? data.evo_pending : prev.evo_pending ?? [],
    dynamic_high_vol_alts: Array.isArray(data.dynamic_high_vol_alts)
      ? data.dynamic_high_vol_alts.map(String)
      : prev.dynamic_high_vol_alts ?? [],
    strategy_profile:
      data.strategy_profile && typeof data.strategy_profile === 'object'
        ? (data.strategy_profile as Status['strategy_profile'])
        : prev.strategy_profile,
  }
}

function applyDashboardPayload(
  data: Record<string, unknown>,
  setStatus: (s: Partial<Status>) => void,
) {
  if (!data || typeof data !== 'object') return
  const prev = useStore.getState().status
  setStatus(normalizeDashboardPayload(data, prev))
}

/** 超过此时长未收到快照/WL 推送则视为断连（与轮询 2.5s 对齐） */
const DATA_STALE_MS = 12_000

export default function App() {
  const { status, connected, setStatus, setConnected } = useStore()
  const wsRef = useRef<WebSocket>()
  const [uptime, setUptime] = useState(0)
  const [currentView, setCurrentView] = useState<AppView>(() => hashToView(window.location.hash))
  const [pollLatencyMs, setPollLatencyMs] = useState<number | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<ConfirmPayload | null>(null)
  const [showEvoPanel, setShowEvoPanel] = useState(false)
  const [showLicenseModal, setShowLicenseModal] = useState(false)
  const [showResetModal, setShowResetModal] = useState(false)
  const [licenseInput, setLicenseInput] = useState('')
  const [resetCapital, setResetCapital] = useState('500')
  const [licenseMsg, setLicenseMsg] = useState('')
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
    for (let i = 0; i < 48; i++) {
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
        ctx!.globalAlpha = Math.max(0.06, s.o)
        ctx!.fill()
      }
      ctx!.globalAlpha = 1
      animId = requestAnimationFrame(draw)
    }
    draw()

    return () => { cancelAnimationFrame(animId); window.removeEventListener('resize', resize) }
  }, [])

  useEffect(() => {
    const syncView = () => setCurrentView(hashToView(window.location.hash))
    window.addEventListener('hashchange', syncView)
    return () => window.removeEventListener('hashchange', syncView)
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
        const r = await fetch(`/api/snapshot${q}`, { cache: 'no-store', headers: dashboardAuthHeaders() })
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
      const qp = new URLSearchParams()
      if (tok) qp.set('token', tok)
      const qs = qp.toString()
      const path = qs ? `/ws?${qs}` : '/ws'
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

  const live = status.live
  const liveActive = live?.active === true
  const paper = status.paper
  const isPaperMode = status.shark_mode !== 'live'
  const tradingOn = isPaperMode
    ? (paper?.trading_enabled === true)
    : (live?.trading_enabled === true)
  /** 价格闪崩熔断或实盘连续报单熔断（与「开始交易」开关独立） */
  const riskHalt =
    status.safety_blocked === true ||
    (status.shark_mode === 'live' && status.live_api_ok === false)
  const showTradeButton =
    connected && (isPaperMode || (status.shark_mode === 'live' && liveActive))

  const runTradeToggle = (expectStart: boolean) => {
    if (expectStart && tradingOn) return
    if (!expectStart && !tradingOn) return
    setConfirmDialog({
      title: expectStart ? '开始交易' : '停止交易',
      message: expectStart
        ? (isPaperMode ? '确认开始模拟盘交易？（纸面资金，不真实下单）' : '确认开始实盘交易？')
        : '确认停止交易并平掉所有持仓？',
      confirmLabel: expectStart ? '开始交易' : '停止交易',
      confirmStyle: expectStart ? 'danger' : 'safe',
      onConfirm: async () => {
        try {
          const j = isPaperMode ? await postPaperToggle() : await postLiveToggle()
          if (j.error) {
            window.alert(`操作失败：${j.error}`)
            return false  // 保持弹窗不关闭
          }
          if (typeof j.trading_enabled === 'boolean') {
            // 即时更新本地状态，不等轮询
            if (isPaperMode) {
              setStatus({ paper: { active: true, trading_enabled: j.trading_enabled } })
            } else {
              setStatus({ live: { ...(live ?? { active: true }), trading_enabled: j.trading_enabled } as LiveStatus })
            }
          }
          // 成功 → ConfirmModal 自动关闭
        } catch (e) {
          console.warn('[shark] trade toggle failed', e)
          window.alert('请求失败，请检查网络')
          return false
        }
      },
    })
  }

  const onModePaper = () => {
    if (status.shark_mode === 'paper') return
    setConfirmDialog({
      title: '切换到模拟盘',
      message:
        '切换到模拟盘（等价 SHARK_MODE=paper）？若有持仓将尝试平仓后再卸載实盘引擎。',
      onConfirm: async () => {
        const e = await postSharkMode('paper')
        if (e.error) window.alert(e.error)
      },
    })
  }

  const onModeLive = () => {
    if (status.shark_mode === 'live') return
    setConfirmDialog({
      title: '切换到实盘',
      message:
        '切换到实盘（等价 SHARK_MODE=live）？需已配置 GATE API；不会自动下单，需再点「开始交易」。',
      confirmLabel: '切换到实盘',
      onConfirm: async () => {
        const e = await postSharkMode('live')
        if (e.error) window.alert(e.error)
      },
    })
  }

  const onTradeButton = () => {
    if (!showTradeButton) return
    if (status.shark_mode === 'live' && !liveActive) return
    runTradeToggle(!tradingOn)
  }

  const onLicenseLogin = async () => {
    const lic = licenseInput.trim()
    if (!lic) { setLicenseMsg('请输入许可证'); return }
    setLicenseMsg('验证中...')
    const r = await postLicenseLogin(lic)
    if (r.ok) {
      try { localStorage.setItem('shark_license', lic) } catch {}
      setLicenseMsg('✅ 登录成功')
      setShowLicenseModal(false)
      setLicenseInput('')
    } else {
      setLicenseMsg(`❌ ${r.reason || '无效许可证'}`)
    }
  }

  const onResetPaper = async () => {
    const cap = parseFloat(resetCapital)
    if (!isFinite(cap) || cap < 50) { window.alert('资金最低 50'); return }
    const r = await postPaperReset(cap)
    if (r.error) { window.alert(r.error); return }
    if (r.ok) {
      window.alert(`模拟盘已重置，初始资金 $${cap}`)
      setShowResetModal(false)
    }
  }

  return (
    <>
    <ConfirmModal
      open={confirmDialog !== null}
      title={confirmDialog?.title ?? ''}
      message={confirmDialog?.message ?? ''}
      confirmLabel={confirmDialog?.confirmLabel}
      confirmStyle={confirmDialog?.confirmStyle}
      onConfirm={confirmDialog?.onConfirm ?? (() => {})}
      onCancel={() => setConfirmDialog(null)}
    />
    {/* 进化审批面板 — 复用 ConfirmModal 格式 */}
    {showEvoPanel && (
      <div className="shark-confirm-overlay" role="presentation" onClick={(e) => { if (e.target === e.currentTarget) setShowEvoPanel(false) }}>
        <div className="shark-confirm-panel" role="dialog" aria-modal="true" style={{ maxWidth: 480 }} onClick={(e) => e.stopPropagation()}>
          <div className="shark-confirm-title" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>自进化审批</span>
            <button onClick={() => setShowEvoPanel(false)} style={{ background: 'none', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', fontSize: 16 }}>✕</button>
          </div>
          <div className="shark-confirm-body" style={{ maxHeight: 360, overflowY: 'auto' }}>
            {(status.evo_pending?.length ?? 0) === 0 ? (
              <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 20 }}>暂无待审批的进化修改</div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {status.evo_pending!.map((c) => (
                  <div key={c.id} style={{
                    padding: '10px 12px', borderRadius: 6,
                    background: 'var(--bg-card)', border: '1px solid var(--border-glass)',
                    fontSize: 12,
                  }}>
                    <div style={{ color: 'var(--accent-cyan)', fontWeight: 700, marginBottom: 4 }}>#{c.id} {c.type}</div>
                    <div style={{ color: 'var(--text-primary)', marginBottom: 6 }}>{c.description}</div>
                    <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                      <button className="btn-live-trade btn-live-trade--start" style={{ fontSize: 10, padding: '3px 12px' }}
                        onClick={async () => {
                          const r = await postEvoApprove(c.id)
                          if (r.error) { window.alert(r.error); return }
                          setStatus({ evo_pending: (status.evo_pending ?? []).filter(x => x.id !== c.id) })
                        }}
                      >通过</button>
                      <button className="btn-modal-cancel" style={{ fontSize: 10, padding: '3px 12px' }}
                        onClick={async () => {
                          const r = await postEvoReject(c.id)
                          if (r.error) { window.alert(r.error); return }
                          setStatus({ evo_pending: (status.evo_pending ?? []).filter(x => x.id !== c.id) })
                        }}
                      >拒绝</button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    )}
    {/* 许可证登录弹窗 */}
    {showLicenseModal && (
      <div className="shark-confirm-overlay" role="presentation" onClick={(e) => { if (e.target === e.currentTarget) setShowLicenseModal(false) }}>
        <div className="shark-confirm-panel" role="dialog" aria-modal="true" style={{ maxWidth: 400 }} onClick={(e) => e.stopPropagation()}>
          <div className="shark-confirm-title">🔑 输入许可证</div>
          <div className="shark-confirm-body">
            <input
              type="text"
              value={licenseInput}
              onChange={(e) => setLicenseInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && onLicenseLogin()}
              placeholder="粘贴许可证 token..."
              autoFocus
              style={{
                width: '100%', padding: '8px 12px', borderRadius: 6,
                background: 'var(--bg-card)', border: '1px solid var(--border-glass)',
                color: 'var(--text-primary)', fontFamily: 'var(--font-mono)', fontSize: 12,
                outline: 'none',
              }}
            />
            {licenseMsg && (
              <div style={{ marginTop: 8, fontSize: 12, color: licenseMsg.startsWith('✅') ? '#0f8' : '#f44' }}>
                {licenseMsg}
              </div>
            )}
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '12px 0 0' }}>
            <button className="btn-modal-cancel" onClick={() => setShowLicenseModal(false)}>取消</button>
            <button className="btn-live-trade btn-live-trade--start" onClick={onLicenseLogin}>验证</button>
          </div>
        </div>
      </div>
    )}
    {/* 重置模拟盘弹窗 */}
    {showResetModal && (
      <div className="shark-confirm-overlay" role="presentation" onClick={(e) => { if (e.target === e.currentTarget) setShowResetModal(false) }}>
        <div className="shark-confirm-panel" role="dialog" aria-modal="true" style={{ maxWidth: 400 }} onClick={(e) => e.stopPropagation()}>
          <div className="shark-confirm-title">🔄 重置模拟盘</div>
          <div className="shark-confirm-body">
            <p style={{ color: 'var(--text-secondary)', fontSize: 12, margin: '0 0 12px' }}>
              重置将清空所有持仓、交易历史和数据库记录。
            </p>
            <label style={{ fontSize: 12, color: 'var(--text-secondary)' }}>初始资金 ($)</label>
            <input
              type="number"
              value={resetCapital}
              onChange={(e) => setResetCapital(e.target.value)}
              min="50"
              max="1000000"
              style={{
                width: '100%', padding: '8px 12px', borderRadius: 6,
                background: 'var(--bg-card)', border: '1px solid var(--border-glass)',
                color: 'var(--text-primary)', fontFamily: 'var(--font-mono)', fontSize: 14,
                outline: 'none', marginTop: 4,
              }}
            />
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '12px 0 0' }}>
            <button className="btn-modal-cancel" onClick={() => setShowResetModal(false)}>取消</button>
            <button className="btn-live-trade btn-live-trade--stop" onClick={onResetPaper}>重置</button>
          </div>
        </div>
      </div>
    )}
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* 顶栏 */}
      <div className="topbar">
        <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 4 }}>
          <div className="topbar-brand">
            <span className="accent">Shark 2.0</span>
          </div>
          <TopbarSectionLinks currentView={currentView} />
        </div>
        <div className="topbar-right">
          <div className="topbar-controls-trading">
            <div className="mode-switch" role="group" aria-label="交易模式">
              <button
                type="button"
                className={isPaperMode ? 'mode-switch--on' : ''}
                onClick={onModePaper}
              >
                模拟盘
              </button>
              <button
                type="button"
                className={!isPaperMode ? 'mode-switch--on-live' : ''}
                onClick={onModeLive}
              >
                实盘
              </button>
            </div>
            {showTradeButton ? (
              <button
                type="button"
                className={`btn-live-trade ${tradingOn ? 'btn-live-trade--stop' : 'btn-live-trade--start'}`}
                onClick={onTradeButton}
              >
                {tradingOn ? '停止交易' : '开始交易'}
              </button>
            ) : null}
            {isPaperMode && (
              <button
                type="button"
                className="btn-live-trade btn-live-trade--stop"
                onClick={() => setShowResetModal(true)}
                title="重置模拟盘资金和记录"
                style={{ fontSize: 11 }}
              >
                重置
              </button>
            )}
            <button
              type="button"
              className="btn-live-trade btn-live-trade--start"
              onClick={() => { setLicenseInput(''); setLicenseMsg(''); setShowLicenseModal(true) }}
              title="输入许可证"
              style={{ fontSize: 11 }}
            >
              登录
            </button>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            {(status.evo_pending?.length ?? 0) > 0 && (
              <button
                onClick={() => setShowEvoPanel(true)}
                style={{
                  background: 'rgba(176,38,255,0.15)', border: '1px solid rgba(176,38,255,0.4)',
                  borderRadius: 9999, padding: '2px 10px', cursor: 'pointer',
                  fontSize: 10, fontWeight: 700, color: 'var(--accent-purple)',
                  fontFamily: 'var(--font-display)',
                }}
              >
                进化 {status.evo_pending!.length}
              </button>
            )}
            <span className={`status-dot ${riskHalt ? 'blocked' : connected ? 'live' : 'disconnected'}`} />
            <span style={{ fontSize: '11px', color: 'var(--text-secondary)', fontWeight: 500 }}>
              {riskHalt ? (status.safety_blocked ? '熔断' : '实盘API熔断') : connected ? '运行中' : '离线'}
            </span>
          </div>
          <Clock />
        </div>
      </div>

      {/* 主内容 */}
      <div style={{ flex: 1, padding: '16px 20px', maxWidth: '1440px', margin: '0 auto', width: '100%' }}>
        {currentView === 'support' ? (
          <SupportPage />
        ) : currentView === 'risk' ? (
          <RiskPage
            status={status}
            riskHalt={riskHalt}
            connected={connected}
            pollLatencyMs={pollLatencyMs}
            fmtUptime={fmtUptime}
          />
        ) : (
          <>
            <div id="section-kpi">
              <Dashboard
                equity={status.equity}
                balance={status.balance}
                freeCash={status.free_cash}
                realizedPnl={status.realized_pnl}
                winRate={status.win_rate}
                positions={status.positions}
                equityChange={equityChange}
                safetyBlocked={riskHalt}
                totalFees={status.total_fees}
                marginLocked={status.margin_locked}
              />
            </div>

            <div className="card" style={{ marginTop: '10px' }} id="section-room">
              <div className="card-header">shark 领域</div>
              <div
                className="card-body"
                style={{
                  padding: '8px 0 0',
                  minHeight: 400,
                  position: 'relative',
                  display: 'flex',
                  flexDirection: 'column',
                }}
              >
                <LoliRoom />
              </div>
            </div>

            <div className="card" style={{ marginTop: '10px' }} id="section-positions">
              <div className="card-header">
                <span>当前持仓 ({status.position_list?.length || 0})</span>
              </div>
              <div className="card-body" style={{ padding: '0' }}>
                <PositionsTable positions={status.position_list || []} />
              </div>
            </div>

            <div className="card" style={{ marginTop: '10px' }} id="section-history">
              <div className="card-header">
                <span>交易记录 ({status.trade_history?.length || 0})</span>
              </div>
              <div className="card-body" style={{ padding: '0' }}>
                <TradeHistory trades={status.trade_history || []} />
              </div>
            </div>
          </>
        )}
      </div>

      {/* 底栏 */}
      <div style={{
        padding: '10px 20px', borderTop: '1px solid var(--border-subtle)',
        fontSize: '10px', color: 'var(--text-muted)',
        display: 'flex', justifyContent: 'space-between',
        background: 'var(--bg-surface)',
      }}>
        <span>Shark 2.0 · AI 多策略量化机器人</span>
        <span style={{ fontFamily: 'var(--font-mono)' }}>
          Gate.io 合约 ·{' '}
          {status.shark_mode === 'live'
            ? (tradingOn ? '实盘下单中' : '实盘 · 未下单')
            : (tradingOn ? '模拟盘 · 交易中' : '模拟盘 · 未交易')}
        </span>
      </div>
    </div>

    </>
  )
}
