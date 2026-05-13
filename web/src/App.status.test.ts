import { describe, expect, it } from 'vitest'
import { normalizeDashboardPayload } from './App'
import type { Status } from './store/useStore'

const prevStatus: Status = {
  equity: 100,
  balance: 100,
  free_cash: 100,
  positions: 0,
  realized_pnl: 0,
  win_rate: 0,
  safety_blocked: false,
  mode: 'Paper',
  initial_capital: 100,
  unrealized_pnl: 0,
  position_list: [],
  live_prices: {},
  total_fees: 0,
  total_slippage: 0,
  trade_history: [],
  margin_locked: 0,
  live: undefined,
  paper: { active: true, trading_enabled: false },
  shark_mode: 'paper',
}

describe('dashboard trading status normalization', () => {
  it('maps legacy paper_trading snapshots into the paper status object', () => {
    const next = normalizeDashboardPayload(
      { paper_trading: true, shark_mode: 'paper' },
      prevStatus,
    )

    expect(next.paper).toEqual({ active: true, trading_enabled: true })
  })
})
