// The Profile page's certifications editor: edits only `certifications`, and a save carries every other key along.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CertificationsEditor, SECTION_ORDER, makeMutators } from '../screens/ResumeSections'

const data = { summary: 'x', certifications: [{ title: 'Google Network Security Specialization', description: 'Google' }] }

describe('certifications editor', () => {
  it('is not a résumé-editor section (SECTION_ORDER is shared with the résumé editor)', () => {
    expect(SECTION_ORDER).not.toContain('Certifications')
  })

  it('shows each certification and an add button', () => {
    render(<CertificationsEditor data={data} setField={vi.fn()} mutate={vi.fn()} onRemoved={vi.fn()} />)
    expect(screen.getByDisplayValue('Google Network Security Specialization')).toBeTruthy()
    expect(screen.getByText('+ Add certification')).toBeTruthy()
  })

  it('editing and adding go through setField / mutate and keep the rest of the document', () => {
    const saved = []
    const { mutate, setField } = makeMutators(data, (next) => saved.push(next))
    render(<CertificationsEditor data={data} setField={setField} mutate={mutate} onRemoved={vi.fn()} />)
    fireEvent.change(screen.getByDisplayValue('Google'), { target: { value: 'Google Cloud' } })
    expect(saved.at(-1).certifications[0].description).toBe('Google Cloud')
    expect(saved.at(-1).summary).toBe('x')
    fireEvent.click(screen.getByText('+ Add certification'))
    expect(saved.at(-1).certifications).toHaveLength(2)
  })
})
