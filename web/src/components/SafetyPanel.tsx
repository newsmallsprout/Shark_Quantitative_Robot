interface Props {
  blocked: boolean
  connected: boolean
  /** 最近一次 /api/snapshot 往返 ms，失败为 null */
  latencyMs: number | null
  /** 与顶栏一致的页面会话时长 HH:MM:SS */
  uptimeLabel: string
}

const BREAKERS: Array<{ name: string; limit: string; status: string }> = [
  { name: '单日亏损', limit: '8%', status: 'OK' },
  { name: '最大回撤', limit: '15%', status: 'OK' },
  { name: '连续亏损', limit: '5次', status: 'OK' },
  { name: 'API 错误', limit: '3次/分', status: 'OK' },
]

export default function SafetyPanel({ blocked, connected, latencyMs, uptimeLabel }: Props) {
  if (blocked) {
    BREAKERS[0].status = '触发'
  } else {
    BREAKERS[0].status = '正常'
  }

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
            {blocked ? '风控熔断' : '一切正常'}
          </div>
          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '2px' }}>
            {blocked ? '交易已暂停 — 检查熔断器' : '熔断器已就绪，持续监控中'}
          </div>
        </div>
      </div>

      {/* 熔断器列表 */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
        {BREAKERS.map((b) => (
          <div key={b.name} className="breaker-row">
            <div>
              <div style={{ color: 'var(--text-primary)' }}>{b.name}</div>
              <div style={{ fontSize: '10px', color: 'var(--text-muted)' }}>阈值: {b.limit}</div>
            </div>
            <span className="breaker-status" style={{
              background: b.status === '触发' ? 'var(--loss-bg)' : 'var(--profit-bg)',
              color: b.status === '触发' ? 'var(--loss)' : 'var(--profit)',
            }}>
              {b.status}
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
