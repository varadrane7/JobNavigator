// The page footer switch (from #24, by @mrAlishah; PDF and Word): opt-in, and only on a job-linked tailored copy.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SECTION_ORDER, SectionEditor, SettingsEditor, resumeSectionNames } from '../screens/ResumeSections'

describe('résumé footer settings', () => {
  it('Settings is a résumé-editor section only for a job-linked tailored copy', () => {
    expect(SECTION_ORDER).not.toContain('Settings')   // Persona edits SECTION_ORDER
    expect(resumeSectionNames({ is_base: false, job_id: 'j1' }).at(-1)).toBe('Settings')
    expect(resumeSectionNames({ is_base: true, job_id: null })).not.toContain('Settings')
    expect(resumeSectionNames({ is_base: true, job_id: 'j1' })).not.toContain('Settings')
    expect(resumeSectionNames({ is_base: false, job_id: null })).not.toContain('Settings')   // freeform tailor
    expect(resumeSectionNames(null)).toBe(SECTION_ORDER)
  })

  it('the switch is off without the flag and turns it on', () => {
    const setField = vi.fn()
    render(<SettingsEditor data={{}} setField={setField} />)
    const sw = screen.getByRole('switch', { name: /Page footer/ })
    expect(sw.getAttribute('aria-checked')).toBe('false')
    fireEvent.click(sw)
    expect(setField).toHaveBeenCalledWith('footer_enabled', true)
  })

  it('an enabled footer shows on and turns off through SectionEditor', () => {
    const setField = vi.fn()
    render(<SectionEditor name="Settings" data={{ footer_enabled: true }} setField={setField} />)
    const sw = screen.getByRole('switch', { name: /Page footer/ })
    expect(sw.getAttribute('aria-checked')).toBe('true')
    fireEvent.click(sw)
    expect(setField).toHaveBeenCalledWith('footer_enabled', false)
  })
})
