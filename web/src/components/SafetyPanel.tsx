import type { Status } from '../store/useStore'

interface Props {
  status: Status
  blocked: boolean
  connected: boolean
  /** 最近一次 /api/snapshot 往返 ms，失败为 null */
  latencyMs: number | null
  /** 与顶栏一致的页面会话时长 HH:MM:SS */
  uptimeLabel: string
}

function uniqueRiskTags(status: Status): string {
  const tags = (status.position_list || [])
    .map((p) => p.entry_risk_tag)
    .filter((v): v is string => Boolean(v && v.trim()))
  if (tags.length === 0) return '等待开仓'
  return Array.from(new Set(tags)).join(' / ')
}

function lastCloseReason(status: Status): string {
  const last = status.trade_history?.[status.trade_history.length - 1]
  if (!last?.reason) return '暂无'
  return String(last.reason)
}

function pct(v: unknown, fallback: string): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return fallback
  return `${Math.round(v * 100)}%`
}

function altPoolLabel(status: Status): string {
  const alts = status.dynamic_high_vol_alts || []
  if (alts.length === 0) return '等待接口刷新'
  return alts.slice(0, 6).map((s) => s.replace('/USDT', '')).join(' / ') + (alts.length > 6 ? ` +${alts.length - 6}` : '')
}

function planningLabel(status: Status): string {
  const planning = status.planning_status
  if (!planning?.active) return '计划已就绪'
  const done = typeof planning.done === 'number' ? planning.done : 0
  const total = typeof planning.total === 'number' && planning.total > 0 ? planning.total : 0
  const progress = total > 0 ? ` ${done}/${total}` : ''
  return `${planning.symbol ? planning.symbol.replace('/USDT', '') : '全量'}${progress}`
}

function openHoldLabel(status: Status): string {
  const b = status.last_tick_block
  if (!b?.detail) return '可新开仓'
  return `${b.code}: ${b.detail}`
}

function openHoldTone(status: Status): 'ok' | 'hot' | 'bad' {
  if (status.safety_blocked || (status.shark_mode === 'live' && status.live_api_ok === false)) return 'bad'
  if (status.last_tick_block?.detail) return 'hot'
  return 'ok'
}

export default function SafetyPanel({ status, blocked, connected, latencyMs, uptimeLabel }: Props) {
  const isLive = status.shark_mode === 'live'
  const tradingOn = isLive
    ? status.live?.trading_enabled === true
    : status.paper?.trading_enabled === true
  const modeLabel = `${isLive ? '实盘' : '模拟盘'} · ${tradingOn ? '交易中' : '待命'}`
  const riskTags = uniqueRiskTags(status)
  const activePositions = status.position_list?.length || 0
  const latestReason = lastCloseReason(status)
  const profile = status.strategy_profile || {}
  const planning = status.planning_status
  const planningActive = planning?.active === true
  const altTtlMin = Math.round((profile.alt_plan_ttl_sec || 600) / 60)
  const strategyRows = [
    { name: '新开仓闸门', desc: '熔断 / 实盘API / 余额等（持仓管理仍运行）', value: openHoldLabel(status), tone: openHoldTone(status) },
    { name: '执行模式', desc: planningActive ? (planning?.message || '正在重做计划') : '双轨资金池', value: planningActive ? planningLabel(status) : modeLabel, tone: planningActive ? 'hot' : tradingOn ? 'hot' : 'ok' },
    {
      name: '主流策略',
      desc: profile.stable_profile || 'BTC/ETH/SOL 中长线重仓',
      value: `${pct(profile.stable_capital_pct, '60%')} · 三仓`,
      tone: 'ok',
    },
    {
      name: '山寨策略',
      desc: profile.alt_profile || '动态热门高波动山寨，方向没坏可扛',
      value: `${pct(profile.alt_capital_pct, '40%')} · ${altTtlMin}分钟刷新`,
      tone: 'hot',
    },
    { name: '动态山寨池', desc: '来自 Gate 热门波动合约接口', value: altPoolLabel(status), tone: 'hot' },
    { name: '当前风控档', desc: '主流重仓 / 山寨进攻 / 入场带', value: riskTags, tone: riskTags === '等待开仓' ? 'idle' : 'hot' },
    { name: '重规划规则', desc: '价格漂移、区间外、连损', value: planningActive ? '正在执行' : '主流+山寨都启用', tone: planningActive ? 'hot' : 'ok' },
    { name: '最近平仓', desc: `${status.trade_history?.length || 0} 条记录`, value: latestReason, tone: latestReason.includes('止损') ? 'bad' : 'idle' },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
      {/* 主状态 */}
      <div style={{
        display: 'flex', alignItems: 'flex-start', gap: '10px',
        padding: '12px', borderRadius: 'var(--radius-md)',
        background: blocked ? 'var(--loss-bg)' : 'var(--profit-bg)',
        border: `1px solid ${blocked ? 'rgba(244,63,94,0.2)' : 'rgba(0,212,170,0.2)'}`,
      }}>
        <span className={`status-dot ${blocked ? 'blocked' : connected ? 'live' : 'disconnected'}`} />
        <div>
          <div style={{
            fontSize: '13px', fontWeight: 700,
            color: blocked ? 'var(--loss)' : 'var(--profit)',
          }}>
            {blocked ? '熔断暂停' : planningActive ? '计划重建中' : '策略运行面板'}
          </div>
          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '2px' }}>
            {blocked ? '当前不允许新开仓' : planningActive ? (planning?.message || '全量计划生成中，等待新计划落地') : `持仓 ${activePositions} · ${modeLabel}`}
          </div>
          {!blocked && status.last_tick_block?.detail ? (
            <div style={{ fontSize: '10px', color: 'var(--warn)', marginTop: '6px', lineHeight: 1.35 }}>
              提示：{status.last_tick_block.detail}
            </div>
          ) : null}
        </div>
      </div>

      {/* 当前策略状态 */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
        {strategyRows.map((b) => (
          <div key={b.name} className="breaker-row">
            <div>
              <div style={{ color: 'var(--text-primary)' }}>{b.name}</div>
              <div style={{ fontSize: '10px', color: 'var(--text-muted)' }}>{b.desc}</div>
            </div>
            <span className="breaker-status" style={{
              background: b.tone === 'bad' ? 'var(--loss-bg)' : b.tone === 'hot' ? 'rgba(255,176,32,0.12)' : 'var(--profit-bg)',
              color: b.tone === 'bad' ? 'var(--loss)' : b.tone === 'hot' ? 'var(--warn)' : 'var(--profit)',
            }}>
              {b.value}
            </span>
          </div>
        ))}
      </div>

      {/* 连接状态 */}
      <div style={{
        borderTop: '1px solid var(--border-subtle)',
        paddingTop: '10px',
        display: 'flex', flexDirection: 'column', gap: '6px',
        fontSize: '11px',
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--text-secondary)' }}>连接状态</span>
          <span style={{
            color: connected ? 'var(--profit)' : 'var(--text-muted)',
            fontWeight: 600,
          }}>
            {connected ? '● 已连接' : '○ 断开'}
          </span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--text-secondary)' }}>延迟</span>
          <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
            {latencyMs != null ? `~${latencyMs}ms` : '—'}
          </span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <span style={{ color: 'var(--text-secondary)' }}>运行时长</span>
          <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{uptimeLabel}</span>
        </div>
      </div>
    </div>
  )
}
