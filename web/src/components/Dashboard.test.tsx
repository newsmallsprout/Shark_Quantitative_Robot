import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import Dashboard from './Dashboard'

const baseProps = {
  equity: 10000,
  balance: 9000,
  freeCash: 8000,
  realizedPnl: 12.34,
  winRate: 0.52,
  positions: 2,
  equityChange: 100,
  safetyBlocked: false,
  totalFees: 1.2,
  marginLocked: 50,
}

describe('Dashboard Pro Desk KPI table', () => {
  it('renders a single KPI table with header and value row', () => {
    render(<Dashboard {...baseProps} />)
    const table = screen.getByRole('table', { name: /kpi overview/i })
    expect(table).toBeInTheDocument()
    const rows = within(table).getAllByRole('row')
    const headerRow = rows[0]
    expect(within(headerRow).getByText('总权益')).toBeInTheDocument()
    expect(within(headerRow).getByText(/可用/i)).toBeInTheDocument()
    expect(rows.length).toBeGreaterThanOrEqual(2)
  })
})
