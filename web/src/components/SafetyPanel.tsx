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

export default function SafetyPanel({ status, blocked, connected, latencyMs, uptimeLabel }: Props) {
  const isLive = status.shark_mode === 'live'
  const tradingOn = isLive
    ? status.live?.trading_enabled === true
    : status.paper?.trading_enabled === true
  const modeLabel = `${isLive ? '实盘' : '模拟盘'} · ${tradingOn ? '交易中' : '待命'}`
  const riskTags = uniqueRiskTags(status)
  const activePositions = status.position_list?.length || 0
  const latestReason = lastCloseReason(status)
  const strategyRows = [
    { name: '执行模式', desc: 'RangePlan 驱动', value: modeLabel, tone: tradingOn ? 'hot' : 'ok' },
    { name: '入场策略', desc: '大区间可开，偏离入场带自动降档', value: '激进快开', tone: 'hot' },
    { name: '当前风控档', desc: '入场带/追单降档/反向区探单', value: riskTags, tone: riskTags === '等待开仓' ? 'idle' : 'hot' },
    { name: '激进单止盈', desc: '不在专属入场带内的单子', value: '手续费 3 倍即跑', tone: 'ok' },
    { name: '连损处理', desc: '单币对同方向连续止损 3 次', value: '强制重规划+探单', tone: 'ok' },
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
            {blocked ? '熔断暂停' : '策略运行面板'}
          </div>
          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '2px' }}>
            {blocked ? '当前不允许新开仓' : `持仓 ${activePositions} · ${modeLabel}`}
          </div>
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
