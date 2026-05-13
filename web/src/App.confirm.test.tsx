import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ConfirmModal } from './App'

describe('ConfirmModal', () => {
  it('stays open when confirm handler returns false', async () => {
    render(
      <ConfirmModal
        open
        title="开始交易"
        message="确认开始？"
        confirmLabel="开始交易"
        onCancel={() => {}}
        onConfirm={() => false}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '开始交易' }))

    expect(screen.getByRole('dialog', { name: '开始交易' })).toBeInTheDocument()
  })
})
