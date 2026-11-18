import { describe, it, expect } from 'vitest'
import { ageTag, likesTag } from '../screens/Emails'

describe('Emails tags', () => {
  it('flags age against the one-day target', () => {
    const now = new Date().toISOString()
    expect(ageTag({ posted_at: now, checks: { age_hours: 3 } })[1]).toBe('good')
    expect(ageTag({ posted_at: now, checks: { age_hours: 30 } })[1]).toBe('warn')
    expect(ageTag({ checks: { age_hours: null } })[0]).toBe('Age unknown')
  })
  it('flags likes against the 100-like target', () => {
    expect(likesTag({ likes: 99 })).toEqual(['99 likes', 'good', 'Under 100 likes'])
    expect(likesTag({ likes: 1 })[0]).toBe('1 like')
    expect(likesTag({ likes: 100 })[1]).toBe('warn')
    expect(likesTag({ likes: null })).toBeNull()
  })
})
