import React, { useEffect, useState } from 'react'
import { Button, Drawer, Heading, Helper, Input, Label, Link, Mono, Notice, Select, Tag } from '../ui'
import { getItem, reprepareItem, getQuestions, answerQuestion, errText } from './api'

const skillsText = (s) => {
  if (!s) return ''
  if (Array.isArray(s)) return s.join(', ')
  if (typeof s === 'object') return Object.entries(s).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : v}`).join('\n')
  return String(s)
}

function Compare({ label, before, after }) {
  const same = (before || '') === (after || '')
  return (
    <div style={{ marginBottom: 16 }}>
      <Label>{label}{after != null && same ? ' (unchanged)' : ''}</Label>
      <div style={{ display: 'grid', gridTemplateColumns: after != null ? '1fr 1fr' : '1fr', gap: 10, marginTop: 6 }}>
        <div>
          <Helper size="sm">Base</Helper>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 'var(--t-12)', margin: 0 }}>{before || '—'}</pre>
        </div>
        {after != null && (
          <div>
            <Helper size="sm">Tailored</Helper>
            <pre style={{ whiteSpace: 'pre-wrap', fontSize: 'var(--t-12)', margin: 0, color: same ? undefined : 'var(--heading-ink)' }}>{after}</pre>
          </div>
        )}
      </div>
    </div>
  )
}

// One parked question: pick from the form's own options when it had any, else free text.
function QuestionRow({ q, onAnswered, push }) {
  const [val, setVal] = useState('')
  const [busy, setBusy] = useState(false)
  const opts = (q.options || []).filter((o) => o && !/^select/i.test(o))
  const save = async () => {
    if (!val.trim()) return
    setBusy(true)
    try {
      const r = await answerQuestion(q.id, val.trim())
      push?.({ kind: 'success', msg: r.item_requeued ? 'Saved to Q&A bank — item queued to submit again' : 'Saved to Q&A bank' })
      onAnswered()
    } catch (e) {
      push?.({ kind: 'error', msg: errText(e, 'Could not save answer') })
    } finally { setBusy(false) }
  }
  return (
    <div style={{ marginBottom: 12 }}>
      <Helper>{q.question}</Helper>
      <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
        {opts.length
          ? <Select value={val} onPick={setVal} placeholder="Choose…" options={opts.map((o) => [o, o])} width={360} />
          : <Input value={val} onChange={(e) => setVal(e.target.value)} placeholder="Your answer" width={360} />}
        <Button size="sm" onClick={save} busy={busy} disabled={!val.trim()}>Save</Button>
      </div>
    </div>
  )
}

export default function ItemDrawer({ itemId, onClose, onChanged, push }) {
  const [d, setD] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [questions, setQuestions] = useState([])

  const loadQuestions = () => getQuestions({ item_id: itemId }).then(setQuestions).catch(() => setQuestions([]))
  useEffect(() => {
    setD(null); setErr('')
    getItem(itemId).then(setD).catch((e) => setErr(errText(e, 'Could not load item')))
    loadQuestions()
  }, [itemId])

  const reprepare = async () => {
    setBusy(true)
    try {
      await reprepareItem(itemId)
      push?.({ kind: 'progress', msg: 'Rebuilding documents…' })
      onChanged?.()
      onClose()
    } catch (e) {
      push?.({ kind: 'error', msg: errText(e, 'Could not re-prepare') })
    } finally { setBusy(false) }
  }

  return (
    <Drawer width={760} onClose={onClose} title="Application">
      <div style={{ padding: '18px 22px', overflowY: 'auto' }}>
        {!d ? <Helper>{err || 'Loading…'}</Helper> : (
          <>
            <Heading strong>{d.company} — {d.title}</Heading>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '8px 0 16px', flexWrap: 'wrap' }}>
              <Tag>{d.status.replace('_', ' ')}</Tag>
              <Tag tone="neutral">{d.submitter}</Tag>
              <Mono size="sm">fit {d.score != null ? Math.round(d.score) : '–'} · {d.base_resume_name}</Mono>
              <Link href={d.url} target="_blank" rel="noopener noreferrer">posting ↗</Link>
            </div>
            {d.error && <Notice tone="bad" style={{ marginBottom: 12 }}>{d.error}</Notice>}
            {(d.flags || []).map((f) => <Notice key={f} tone="warn" style={{ marginBottom: 8 }}>{f}</Notice>)}

            {questions.length > 0 && (
              <div style={{ marginBottom: 18 }}>
                <Label>Questions the form needs you to answer</Label>
                <Helper size="sm" style={{ marginBottom: 8 }}>Answers are saved to your Persona Q&amp;A bank and reused on every later form.</Helper>
                {questions.map((q) => (
                  <QuestionRow key={q.id} q={q} push={push} onAnswered={() => { loadQuestions(); onChanged?.() }} />
                ))}
              </div>
            )}

            <Compare label="Summary" before={d.base?.summary} after={d.tailored ? d.tailored.summary : null} />
            <Compare label="Skills" before={skillsText(d.base?.skills)} after={d.tailored ? skillsText(d.tailored.skills) : null} />
            {!d.tailored && <Helper>Not tailored yet — documents are built once the item is approved.</Helper>}

            <div style={{ display: 'flex', gap: 8, margin: '12px 0 18px', flexWrap: 'wrap' }}>
              {d.has_resume_pdf && <Button variant="secondary" size="sm" href={`/api/autoapply/items/${d.id}/file/resume`} target="_blank">Résumé PDF</Button>}
              {d.has_cover_letter_pdf && <Button variant="secondary" size="sm" href={`/api/autoapply/items/${d.id}/file/cover_letter`} target="_blank">Cover letter PDF</Button>}
              {d.tailored_resume_id && <Button variant="ghost" size="sm" href={`/resumes/${d.tailored_resume_id}`}>Edit tailored résumé</Button>}
              {['ready', 'failed', 'needs_human', 'dry_run_ok', 'approved'].includes(d.status) && (
                <Button variant="ghost" size="sm" busy={busy} onClick={reprepare}>Rebuild documents</Button>
              )}
            </div>

            {(d.attempts_log || []).length > 0 && (
              <>
                <Label>Submit attempts</Label>
                {d.attempts_log.map((a) => (
                  <div key={a.id} style={{ margin: '8px 0 14px' }}>
                    <Mono size="sm">{a.started_at} · {a.outcome || 'running'}{a.dry_run ? ' · dry run' : ''}</Mono>
                    {a.confirmation_text && <Helper size="sm">{a.confirmation_text}</Helper>}
                    {Object.keys(a.receipt || {}).length > 0 && (
                      <table style={{ fontSize: 'var(--t-12)', marginTop: 6, borderCollapse: 'collapse' }}>
                        <tbody>
                          {Object.entries(a.receipt).map(([k, v]) => (
                            <tr key={k}><td style={{ padding: '2px 10px 2px 0', color: 'var(--muted)' }}>{k}</td><td>{String(v)}</td></tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                    {a.screenshots > 0 && (
                      <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
                        {Array.from({ length: a.screenshots }, (_, i) => (
                          <Link key={i} href={`/api/autoapply/attempts/${a.id}/screenshot/${i}`} target="_blank">screenshot {i + 1}</Link>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </>
            )}
          </>
        )}
      </div>
    </Drawer>
  )
}
