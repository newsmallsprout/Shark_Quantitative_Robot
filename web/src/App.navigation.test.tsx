import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { TopbarSectionLinks } from './App'

describe('TopbarSectionLinks', () => {
  it('exposes a clickable plans page link', () => {
    render(<TopbarSectionLinks />)

    const plansLink = screen.getByRole('link', { name: /plans/i })
    expect(plansLink).toHaveAttribute('href', '/plans')
  })
})
