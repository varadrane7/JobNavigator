// The People screen shows who each person is (audience), the follow-up state, and the E-Verify flag.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const lead = (o) => ({ id: o.name, name: o.name, status: 'requested', company: 'Acme', title: 'Engineer', score: 50,
  found_on: '2026-11-01', source: 'linkedin_mock', talking_points: [], ...o })
let draftsOn = true
const rows = [
  lead({ name: 'Zed Recruiter', audience: 'recruiter', everify: 'unknown' }),
  lead({ name: 'Ann Alum', audience: 'alum', everify: 'yes',
    follow_up: { n: 1, of: 2, due: true, label: 'follow-up 1 of 2 due' }, last_action_at: '2026-11-10T00:00:00Z' }),
  lead({ name: 'Nora None', audience: null, everify: 'no' }),
  lead({ name: 'Cam Connected', status: 'connected', audience: 'engineer', everify: 'unknown' }),
]

vi.mock('../api', () => ({
  default: {
    get: vi.fn((url) => Promise.resolve({ data: url === '/contacts' ? rows : url === '/monitor/active' ? [] : url === '/contacts/invites' ? { sent_today: 0, cap: 8, remaining: 8, over_cap_by: 0, drafts_enabled: draftsOn } : {} })),
    patch: vi.fn(() => Promise.resolve({ data: {} })),
    post: vi.fn(() => Promise.resolve({ data: {} })),
  },
}))

import People from '../screens/People'

describe('People cards', () => {
  it('tags the audience, the follow-up due and the E-Verify flag', async () => {
    render(<MemoryRouter><People /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('Ann Alum')).toBeTruthy())
    expect(screen.getByText('Alum')).toBeTruthy()
    expect(screen.getByText('follow-up 1 of 2 due')).toBeTruthy()
    expect(screen.getByText('E-Verify: yes')).toBeTruthy()
    expect(screen.getAllByText('E-Verify: unknown').length).toBe(2)
    expect(screen.getByText('E-Verify: no')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Follow-up sent' })).toBeTruthy()
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Draft message' }).length).toBe(2))   // Ann (follow-up due) and Cam (connected)
  })

  it('hides the Draft message control when the profile has drafts turned off', async () => {
    draftsOn = false
    render(<MemoryRouter><People /></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('Cam Connected')).toBeTruthy())
    await waitFor(() => expect(screen.queryAllByRole('button', { name: 'Draft message' }).length).toBe(0))
    expect(screen.getByRole('button', { name: 'Follow-up sent' })).toBeTruthy()
    draftsOn = true
  })
})
