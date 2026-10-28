import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api'
import { useToasts, ToastStack } from '../Toast'
import { Button, Card, HeaderRow, Helper, Link, Mono, PageTitle, Select, Tag, Heading, Notice, Spinner, scoreTone } from '../ui'
import { agoShort } from '../time'
import { fetchRunOutcome, runFailed, runFailureReason } from '../hooks'
import '../theme.css'

// People: the lead pipeline. The lead scout (backend/curator/scout.py) finds people worth knowing
// from the persona (hiring posts, project fit, schools and past employers), independent of jobs;
// the curator adds people about its picks. Each lead moves new → requested → connected →
// messaged → replied → referral, and comes back as "Due" when a step has waited too long.

const KIND = {
  hiring_post: ['Hiring post', 'accent'], project_fit: ['Project fit', 'accent'], alumni: ['Alumni', 'good'],
  founder: ['Founder', 'accent'], job: ['For a Today pick', 'neutral'], network: ['Good company', 'neutral'],
}
const ROLE = { founder: 'Founder', hiring_manager: 'Hiring manager', recruiter: 'Recruiter', engineer: 'Engineer', other: '' }
const STATUS = [['new', 'New'], ['requested', 'Requested'], ['connected', 'Connected'], ['messaged', 'Messaged'],
  ['replied', 'Replied'], ['referral', 'Referral'], ['snoozed', 'Snoozed'], ['skip', 'Skip']]
const NEXT = { new: ['requested', 'Sent request'], requested: ['connected', 'Accepted'], connected: ['messaged', 'Messaged'],
  messaged: ['replied', 'Replied'], replied: ['referral', 'Got referral'] }
const TABS = [['due', 'Due today'], ['new', 'New leads (not contacted)'], ['pipeline', 'In progress'], ['all', 'Everyone']]
const PIPELINE = new Set(['requested', 'connected', 'messaged', 'replied', 'referral'])
const DEGREE = { 1: '1st', 2: '2nd', 3: '3rd+' }

const copy = async (text, push, what) => {
  try { await navigator.clipboard.writeText(text); push({ kind: 'success', msg: `${what} copied` }) }
  catch { push({ kind: 'error', msg: 'Clipboard unavailable' }) }
}

function SessionChip({ which, s, onRefresh }) {
  if (!s) return null
  const cooling = s.cooling_until ? new Date(s.cooling_until) : null
  const spent = s.used >= s.cap
  const tone = cooling ? 'bad' : spent ? 'warn' : 'good'
  const text = cooling ? `cooling until ${cooling.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : `${s.used}/${s.cap} today`
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <Tag tone={tone} title={which === 'mock' ? 'LinkedIn searches used today on the mock (fake) account, which does the searching' : 'LinkedIn searches used today on your real account (only when the mock one is throttled, and for 2nd/3rd checks)'}>{which === 'mock' ? 'Mock account' : 'Your account'} · {text}</Tag>
      {cooling && <Button size="xs" variant="ghost" onClick={onRefresh} title="Sign this session in again (Settings › LinkedIn)">Refresh</Button>}
    </span>
  )
}

const daysSince = (iso) => (iso ? Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)) : null)
// One line on why this person is on the Due list: the step that is waiting.
function dueReason(c) {
  const d = daysSince(c.last_action_at || c.found_on)
  const ago = d == null ? '' : ` ${d} day${d === 1 ? '' : 's'} ago`
  if (c.status === 'new') return 'New lead: send a connection request.'
  if (c.status === 'requested') return `Request sent${ago}, no answer. Accepted on LinkedIn? Press Accepted. Otherwise drop it.`
  if (c.status === 'connected') return `Accepted${ago}: send one message with one ask.`
  if (c.status === 'messaged') return `Messaged${ago}, no reply: nudge once, or drop it.`
  if (c.status === 'snoozed') return 'Snooze is over: decide now.'
  return null
}

function LeadCard({ c, onStatus, push, showDue }) {
  const [open, setOpen] = useState(false)
  const [label, tone] = KIND[c.lead_kind] || [c.lead_kind || 'Lead', 'neutral']
  const next = NEXT[c.status]
  const done = c.status === 'skip' || c.status === 'referral'
  return (
    <Card style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 14px', opacity: done ? 0.6 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, flexWrap: 'wrap' }}>
        <div style={{ minWidth: 0, flex: '1 1 220px', display: 'flex', flexDirection: 'column' }}>
          <Link href={c.linkedin_url} target="_blank" rel="noreferrer">{c.name}</Link>
          <Helper size="xs" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={c.title || ''}>
            {c.title || ''}{c.company ? ` · ${c.company}` : ''}{c.location ? ` · ${c.location}` : ''}
          </Helper>
        </div>
        {c.score != null && <Tag tone={scoreTone(c.score)} title="How worth your time (curator model)">{c.score}</Tag>}
        <Tag tone={tone}>{label}</Tag>
        {ROLE[c.kind] ? <Tag tone="neutral">{ROLE[c.kind]}</Tag> : null}
        {c.degree ? <Tag tone={c.degree === 2 ? 'good' : 'neutral'} title="Distance in your own network">{DEGREE[c.degree]}</Tag> : null}
        {next && <Button size="xs" variant="primary" onClick={() => onStatus(c, next[0])}>{next[1]}</Button>}
        {(c.status === 'requested' || c.status === 'messaged') && <Button size="xs" variant="ghost" onClick={() => onStatus(c, 'skip')} title="They never answered: take them off your list">No reply, drop</Button>}
        <Select value={c.status === 'contacted' ? 'requested' : c.status} options={STATUS} onPick={(v) => onStatus(c, v)} width="120px" ariaLabel="Lead status" />
      </div>
      {showDue && dueReason(c) && <Helper size="xs" style={{ color: 'var(--warn)' }}>{dueReason(c)}</Helper>}
      {c.why && <Helper size="xs">{c.why}</Helper>}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {c.note && <Button size="xs" variant="ghost" onClick={() => copy(c.note, push, 'Connection note')}>Copy note</Button>}
        {c.email_draft && <Button size="xs" variant="ghost" onClick={() => copy(c.email_draft, push, 'Email draft')}>Copy email draft</Button>}
        {c.project_ref && <Helper size="xs">Show: <Mono size="xs">{c.project_ref}</Mono></Helper>}
        {(c.talking_points?.length || c.evidence) ? <Button size="xs" variant="ghost" onClick={() => setOpen((v) => !v)} ariaLabel="Talking points">{open ? 'Less ▴' : 'Talking points ▾'}</Button> : null}
        <Helper size="xs" style={{ marginLeft: 'auto' }}>
          {c.next_action_at ? `due ${c.next_action_at}` : ''}{c.found_on ? ` · found ${agoShort(c.found_on)} ago` : ''} · via {String(c.source || '').replace('_', ' ')}
        </Helper>
      </div>
      {open && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, paddingLeft: 4 }}>
          {(c.talking_points || []).map((t, i) => <Helper key={i} size="xs">• {t}</Helper>)}
          {c.note && <Helper size="xs" style={{ whiteSpace: 'pre-wrap' }}><b>Note:</b> {c.note}</Helper>}
          {c.evidence && <Helper size="xs" style={{ whiteSpace: 'pre-wrap', opacity: 0.8 }}><b>Found from:</b> {c.evidence}</Helper>}
        </div>
      )}
    </Card>
  )
}

export default function People() {
  const navigate = useNavigate()
  const { toasts, push, dismiss } = useToasts()
  const [tab, setTab] = useState('due')
  const [kind, setKind] = useState('')
  const [rows, setRows] = useState(null)
  const [err, setErr] = useState(null)
  const [usage, setUsage] = useState(null)
  const [running, setRunning] = useState(false)

  const load = useCallback(() => {
    const params = { limit: 500 }
    if (tab === 'due') params.due = true
    if (tab === 'new') params.status = 'new'
    if (tab === 'pipeline') params.status = [...PIPELINE].join(',')
    if (kind) params.kind = kind
    api.get('/contacts', { params })
      .then(({ data }) => { setRows(data || []); setErr(null) })
      .catch((e) => setErr(e?.response?.data?.detail || 'Could not load people'))
    api.get('/linkedin/usage').then(({ data }) => setUsage(data)).catch(() => setUsage(null))
  }, [tab, kind])
  useEffect(() => { load() }, [load])

  // a scout run started here (or by the morning cron) keeps the button spinning until it ends
  useEffect(() => {
    let dead = false
    let runId = null
    const tick = async () => {
      try {
        const { data: runs } = await api.get('/monitor/active')
        const live = (runs || []).find((r) => r.job_type === 'network_scout')
        if (dead) return
        if (live) { runId = live.run_id || live.id || runId; setRunning(true) }
        else if (running) {
          setRunning(false)
          const run = runId ? await fetchRunOutcome(runId, 'network_scout') : null
          if (run && runFailed(run)) push({ kind: 'error', msg: `Scout failed — ${runFailureReason(run)}` })
          else if (run?.result_summary) push({ kind: 'success', msg: run.result_summary })
          load()
        }
      } catch { /* the next tick retries */ }
    }
    tick()
    const iv = setInterval(tick, running ? 4000 : 30000)
    return () => { dead = true; clearInterval(iv) }
  }, [running, load, push])

  const scoutNow = async () => {
    try {
      await api.post('/scout/run')
      setRunning(true)
      push({ kind: 'progress', msg: 'Scouting — searching posts and profiles, then scoring; a few minutes' })
    } catch (e) {
      push({ kind: 'error', msg: e?.response?.status === 409 ? 'Already running' : 'Could not start the scout' })
    }
  }

  const setStatus = async (c, status) => {
    try {
      const { data } = await api.patch(`/contacts/${c.id}`, { status })
      setRows((rs) => (tab === 'due' && !data.next_action_at ? rs.filter((x) => x.id !== c.id) : rs.map((x) => (x.id === c.id ? data : x))))
    } catch { push({ kind: 'error', msg: 'Could not update' }) }
  }

  const sorted = useMemo(() => {
    const list = [...(rows || [])]
    if (tab === 'due') return list
    return list.sort((a, b) => (b.score ?? -1) - (a.score ?? -1) || String(b.found_on || '').localeCompare(String(a.found_on || '')))
  }, [rows, tab])
  const counts = useMemo(() => {
    const out = {}
    for (const c of rows || []) out[c.status] = (out[c.status] || 0) + 1
    return out
  }, [rows])

  return (
    <div style={{ position: 'relative', flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 18, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
          <PageTitle>People</PageTitle>
          <Helper>People who can hire, refer or vouch for you. Work "Due today" top to bottom every day.</Helper>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <SessionChip which="mock" s={usage?.mock} onRefresh={() => navigate('/settings#linkedin')} />
          <SessionChip which="main" s={usage?.main} onRefresh={() => navigate('/settings#linkedin')} />
          <Button variant="ai" busy={running} onClick={scoutNow} disabled={running}>{running ? 'Scouting…' : 'Scout now'}</Button>
        </div>
      </HeaderRow>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '10px 30px 0', flexWrap: 'wrap' }}>
        <Select value={tab} options={TABS} onPick={setTab} width="190px" ariaLabel="Which people" />
        <Select value={kind} options={[['', 'Every source'], ...Object.entries(KIND).map(([k, [l]]) => [k, l])]} onPick={setKind} width="170px" ariaLabel="Lead source" />
        {tab === 'all' && rows && <Helper size="xs">{STATUS.filter(([k]) => counts[k]).map(([k, l]) => `${l} ${counts[k]}`).join(' · ')}</Helper>}
      </div>
      <div className="v2-scroll" style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: '12px 30px 40px', display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 1100 }}>
        {err && <Notice tone="bad">{err}</Notice>}
        {!rows && !err && <Spinner />}
        {rows && rows.length === 0 && (
          <Helper>{tab === 'due' ? 'Nothing due. Scout now, or wait for the morning run.' : 'Nobody here yet.'}</Helper>
        )}
        {tab === 'due' && rows?.length > 0 && <Heading strong={600} size={16}>Do these today · {rows.length}</Heading>}
        {sorted.map((c) => <LeadCard key={c.id} c={c} onStatus={setStatus} push={push} showDue={tab === 'due'} />)}
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
