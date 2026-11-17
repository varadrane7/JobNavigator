// The People screen's invite chip: "x/8 invites today", warning at the cap, "over cap by N" beyond it.
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { InviteChip } from '../screens/People'

const chip = (inv) => render(<InviteChip inv={inv} />).container

describe('InviteChip', () => {
  it('shows nothing until the numbers arrive', () => {
    expect(chip(null).textContent).toBe('')
  })
  it('shows sent / cap', () => {
    expect(chip({ sent_today: 3, cap: 8, remaining: 5, over_cap_by: 0, carried: 0 }).textContent).toBe('3/8 invites today')
  })
  it('says how many leads are carried to tomorrow', () => {
    const el = chip({ sent_today: 8, cap: 8, remaining: 0, over_cap_by: 0, carried: 4 })
    expect(el.textContent).toBe('8/8 invites today')
    expect(el.firstChild.getAttribute('title')).toContain('4 more new leads are carried to tomorrow')
  })
  it('flags a record beyond the cap', () => {
    expect(chip({ sent_today: 10, cap: 8, remaining: 0, over_cap_by: 2, carried: 0 }).textContent).toBe('10/8 invites today · over cap by 2')
  })
})
