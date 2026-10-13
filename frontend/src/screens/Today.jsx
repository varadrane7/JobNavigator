import React, { useState, useEffect, useCallback, useMemo } from 'react'
import api from '../api'
import { useToasts, ToastStack } from '../Toast'
import { Button, Card, HeaderRow, Helper, Label, Link, Meter, Mono, PageTitle, Select, Tag, Heading, Notice, Spinner, scoreTone } from '../ui'
import { agoShort, tzDateKey, TZ_SHORT } from '../time'
import { fetchRunOutcome, runFailed, runFailureReason } from '../hooks'
import '../theme.css'

// Today: the daily curator's ranked Top list (backend/curator) and the people to
// contact about it. Rank is the order to apply in; the window says how soon.

const WINDOW = {
  now: ['good', 'Apply now'],
  morning: ['accent', 'This morning'],
  today: ['neutral', 'Today'],
}
const PARTS = [['fit', 'Fit'], ['fresh', 'Fresh'], ['visa_comp', 'Visa / pay'], ['reply', 'Reply odds'], ['stability', 'Stability']]
const KIND = { founder: 'Founder', hiring_manager: 'Hiring manager', recruiter: 'Recruiter', engineer: 'Engineer' }
const CONTACT_STATUS = [['new', 'New'], ['contacted', 'Contacted'], ['replied', 'Replied'], ['skip', 'Skip']]

const copy = async (text, push, what) => {
  try { await navigator.clipboard.writeText(text); push({ kind: 'success', msg: `${what} copied` }) }
  catch { push({ kind: 'error', msg: 'Clipboard unavailable' }) }
}

function JobCard({ j, onStatus }) {
  const [open, setOpen] = useState(false)
  const [tone, label] = WINDOW[j.apply_window] || WINDOW.today
  const done = j.status !== 'saved'
  const it = j.intel || {}
  return (
    <Card style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 14px', opacity: done ? 0.55 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
        <Mono size="lg" style={{ flex: '0 0 28px', textAlign: 'right' }}>{j.rank}</Mono>
        <Tag tone={tone}>{label}</Tag>
        <div style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column' }}>
          <Link href={j.url} target="_blank" rel="noreferrer" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{j.title}</Link>
          <Helper size="xs" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {j.company}{j.location ? ` · ${j.location}` : ''}{j.posted_at ? ` · ${agoShort(j.posted_at)} ago` : ''}
          </Helper>
        </div>
        {j.best_score != null && <Tag tone={scoreTone(j.best_score)} title={`Résumé fit (${j.best_cv || 'best'})`}>{Math.round(j.best_score)}</Tag>}
        {j.h1b_lca_count ? <Tag tone="accent" title="H-1B LCAs filed by this company">H-1B {j.h1b_lca_count}</Tag> : null}
        {it.funding_stage && it.funding_stage !== 'unknown' && <Tag tone="neutral">{it.funding_stage.replace('_', ' ')}</Tag>}
        {done
          ? <Tag tone={j.status === 'applied' ? 'good' : 'neutral'}>{j.status}</Tag>
          : <>
              <Button size="xs" variant="primary" onClick={() => onStatus(j, 'applied')}>Applied</Button>
              <Button size="xs" variant="ghost" onClick={() => onStatus(j, 'skip')}>Skip</Button>
            </>}
        <Button size="xs" variant="ghost" onClick={() => setOpen((v) => !v)} ariaLabel="Why this job">{open ? '▴' : '▾'}</Button>
      </div>
      <Helper size="xs" style={{ paddingLeft: 38 }}>{j.reason}</Helper>
      {open && (
        <div style={{ paddingLeft: 38, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '6px 18px' }}>
          {PARTS.map(([k, name]) => (
            <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
              <Label size="xs">{name} · {Math.round(((j.parts || {})[k] || 0) * 100)}</Label>
              <Meter value={(j.parts || {})[k] || 0} />
            </div>
          ))}
          {it.notes ? <Helper size="xs" style={{ gridColumn: '1 / -1' }}>{it.notes}</Helper> : null}
        </div>
      )}
    </Card>
  )
}

function ContactCard({ c, onStatus, push }) {
  return (
    <Card style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 14px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
        <div style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column' }}>
          <Link href={c.linkedin_url} target="_blank" rel="noreferrer">{c.name}</Link>
          <Helper size="xs" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.title || ''} · {c.company}</Helper>
        </div>
        {c.kind && <Tag tone={c.kind === 'hiring_manager' || c.kind === 'founder' ? 'accent' : 'neutral'}>{KIND[c.kind] || c.kind}</Tag>}
        <Select value={c.status} options={CONTACT_STATUS} onPick={(v) => onStatus(c, v)} width="120px" ariaLabel="Contact status" />
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {c.email
          ? <Helper size="xs" mono title="Guessed from the company's email format — verify before sending">{c.email} (guess)</Helper>
          : <Helper size="xs">No email found</Helper>}
        {c.email && <Button size="xs" variant="ghost" onClick={() => copy(c.email, push, 'Email')}>Copy email</Button>}
        {c.note && <Button size="xs" variant="ghost" onClick={() => copy(c.note, push, 'LinkedIn note')}>Copy note</Button>}
        {c.email_draft && <Button size="xs" variant="ghost" onClick={() => copy(c.email_draft, push, 'Email draft')}>Copy email draft</Button>}
        <Helper size="xs" style={{ marginLeft: 'auto' }}>via {String(c.source || '').replace('_', ' ')}</Helper>
      </div>
    </Card>
  )
}

export default function Today() {
  const { toasts, push, dismiss } = useToasts()
  const [day, setDay] = useState(tzDateKey())
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [running, setRunning] = useState(false)

  const load = useCallback(() => {
    api.get('/curator/today', { params: { day } })
      .then(({ data }) => { setData(data); setErr(null) })
      .catch((e) => setErr(e?.response?.data?.detail || 'Could not load today\'s list'))
  }, [day])
  useEffect(() => { load() }, [load])

  // a run started here (or by the 4 AM cron) keeps the button spinning until it leaves /monitor/active
  useEffect(() => {
    let dead = false
    let runId = null
    const tick = async () => {
      try {
        const { data: runs } = await api.get('/monitor/active')
        const live = (runs || []).find((r) => r.job_type === 'curate')
        if (dead) return
        if (live) { runId = live.run_id || live.id || runId; setRunning(true) }
        else if (running) {
          setRunning(false)
          const run = runId ? await fetchRunOutcome(runId, 'curate') : null
          if (run && runFailed(run)) push({ kind: 'error', msg: `Curation failed — ${runFailureReason(run)}` })
          load()
        }
      } catch { /* the next tick retries */ }
    }
    tick()
    const iv = setInterval(tick, running ? 4000 : 30000)
    return () => { dead = true; clearInterval(iv) }
  }, [running, load, push])

  const runNow = async () => {
    try {
      await api.post('/curator/run')
      setRunning(true)
      push({ kind: 'progress', msg: 'Curating — searching, scoring and ranking; this takes a few minutes' })
    } catch (e) {
      push({ kind: 'error', msg: e?.response?.status === 409 ? 'Already running' : 'Could not start curation' })
    }
  }

  const setJobStatus = async (j, status) => {
    try {
      await api.patch(`/jobs/${j.id}`, status === 'skip' ? { status: 'skip', saved: false } : { status })
      setData((d) => ({ ...d, jobs: d.jobs.map((x) => (x.id === j.id ? { ...x, status } : x)) }))
      window.dispatchEvent(new CustomEvent('jn:counts-changed'))
    } catch { push({ kind: 'error', msg: 'Could not update the job' }) }
  }
  const setContactStatus = async (c, status) => {
    try {
      await api.patch(`/contacts/${c.id}`, { status })
      setData((d) => ({ ...d, contacts: d.contacts.map((x) => (x.id === c.id ? { ...x, status } : x)) }))
    } catch { push({ kind: 'error', msg: 'Could not update the contact' }) }
  }

  const jobs = data?.jobs || []
  const contacts = data?.contacts || []
  const applied = jobs.filter((j) => j.status === 'applied').length
  const dayOpts = useMemo(() => {
    const ds = new Set([tzDateKey(), ...(data?.days || [])])
    return [...ds].sort().reverse().map((d) => [d, d === tzDateKey() ? `Today · ${d}` : d])
  }, [data])

  return (
    <div style={{ position: 'relative', flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 18 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
          <PageTitle>Today</PageTitle>
          <Helper>{jobs.length ? `${jobs.length} picks · ${applied} applied · ${contacts.length} people · ranked in ${TZ_SHORT}` : 'The curator runs every morning and saves the best jobs here'}</Helper>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
          <Select value={day} options={dayOpts} onPick={setDay} width="170px" ariaLabel="Day" />
          <Button variant="ai" busy={running} onClick={runNow} disabled={running}>{running ? 'Curating…' : 'Run now'}</Button>
        </div>
      </HeaderRow>

      <div className="v2-scroll" style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: '16px 30px 40px', display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        {err && <Notice tone="bad" style={{ width: '100%' }}>{err}</Notice>}
        {!data && !err && <Spinner />}
        {data && (
          <>
            <section style={{ flex: '2 1 560px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <Heading strong={600} size={16}>Apply in this order</Heading>
              {jobs.length === 0 && <Helper>No curated jobs for {day}. Run now, or wait for the morning run.</Helper>}
              {jobs.map((j) => <JobCard key={j.id} j={j} onStatus={setJobStatus} />)}
            </section>
            <section style={{ flex: '1 1 340px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <Heading strong={600} size={16}>People to contact</Heading>
              {contacts.length === 0 && <Helper>Nobody found for {day} yet.</Helper>}
              {contacts.map((c) => <ContactCard key={c.id} c={c} onStatus={setContactStatus} push={push} />)}
            </section>
          </>
        )}
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
