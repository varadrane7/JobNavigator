import React, { useState, useEffect, useCallback, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api'
import { useToasts, ToastStack } from '../Toast'
import { Button, Card, HeaderRow, Helper, Label, Link, Meter, Mono, PageTitle, Select, Tag, Heading, Notice, Spinner, scoreTone } from '../ui'
import { agoShort, tzDateKey, TZ_SHORT } from '../time'
import { fetchRunOutcome, runFailed, runFailureReason } from '../hooks'
import '../theme.css'

// Today: the daily curator's ranked Top list (backend/curator), the people to
// contact about it, and "people to know": decision makers at good companies with
// no posting for the user today. Rank is the order to apply in; everything is meant to be done today.
// Applied / skipped jobs and contacted / skipped people sink into a "Done" section at the bottom.

const PARTS = [['fit', 'Fit'], ['fresh', 'Fresh'], ['visa_comp', 'Visa / pay'], ['reply', 'Reply odds'], ['stability', 'Stability']]
const KIND = { founder: 'Founder', hiring_manager: 'Hiring manager', recruiter: 'Recruiter', engineer: 'Engineer' }
// the lead pipeline's first steps; the rest (connected, messaged, replied, referral) live on People
const CONTACT_STATUS = [['new', 'New'], ['requested', 'Requested'], ['skip', 'Skip']]
const CONTACT_DONE = new Set(['requested', 'connected', 'messaged', 'replied', 'referral', 'snoozed', 'skip'])
// the lead scout's people (curator/scout.py) are worked on the People screen, not here
const SCOUTED = new Set(['hiring_post', 'project_fit', 'alumni', 'founder'])
const DIM = 0.55   // dims the card's text, never the card: opacity would trap the status dropdown under later cards
const SORTS = [['rank', 'Rank'], ['fit', 'Fit'], ['newest', 'Newest'], ['company', 'Company']]
const SORT_BY = {
  rank: (a, b) => (a.rank ?? 1e9) - (b.rank ?? 1e9),
  fit: (a, b) => (b.best_score ?? -1) - (a.best_score ?? -1) || (a.rank ?? 1e9) - (b.rank ?? 1e9),
  newest: (a, b) => String(b.posted_at || '').localeCompare(String(a.posted_at || '')),
  company: (a, b) => String(a.company || '').localeCompare(String(b.company || '')) || (a.rank ?? 1e9) - (b.rank ?? 1e9),
}

const copy = async (text, push, what) => {
  try { await navigator.clipboard.writeText(text); push({ kind: 'success', msg: `${what} copied` }) }
  catch { push({ kind: 'error', msg: 'Clipboard unavailable' }) }
}

function JobCard({ j, onStatus, onCoverLetter, onTailor }) {
  const [open, setOpen] = useState(false)
  const done = j.status !== 'saved'
  const it = j.intel || {}
  return (
    <Card style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: done ? '6px 14px' : '10px 14px', opacity: done ? DIM : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
        <Mono size="lg" style={{ flex: '0 0 28px', textAlign: 'right' }}>{j.rank}</Mono>
        <div style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column' }}>
          <Link href={j.url} target="_blank" rel="noreferrer" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{j.title}</Link>
          <Helper size="xs" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {j.company}{j.location ? ` · ${j.location}` : ''}{j.posted_at ? ` · ${agoShort(j.posted_at)} ago` : ''}
          </Helper>
        </div>
        {j.best_score != null && <Tag tone={scoreTone(j.best_score)} title={`Résumé fit (${j.best_cv || 'best'})`}>{Math.round(j.best_score)}</Tag>}
        {j.h1b_lca_count ? <Tag tone="accent" title="H-1B LCAs filed by this company">H-1B {j.h1b_lca_count}</Tag> : null}
        {(j.parts || {}).tracked ? <Tag tone="accent" title="On your Companies list">Your list</Tag> : null}
        {it.funding_stage && it.funding_stage !== 'unknown' && <Tag tone="neutral">{it.funding_stage.replace('_', ' ')}</Tag>}
        {done
          ? <Tag tone={j.status === 'applied' ? 'good' : 'neutral'}>{j.status}</Tag>
          : <>
              <Button size="xs" variant="primary" onClick={() => onStatus(j, 'applied')}>Applied</Button>
              <Button size="xs" variant="ghost" onClick={() => onStatus(j, 'skip')}>Skip</Button>
            </>}
        <Button size="xs" variant="ghost" onClick={() => onTailor(j)} disabled={!j.tailor_base_id}
          title={j.tailor_base_id ? `Tailor a résumé for this job, starting from ${j.best_cv && j.best_cv !== 'Persona' ? j.best_cv : 'General'}` : 'No base résumé to tailor from'}>Tailor</Button>
        <Button size="xs" variant="ghost" onClick={() => onCoverLetter(j)}
          title={j.best_cv ? `Cover letter for this job, from ${j.best_cv}` : 'Cover letter for this job'}>Cover letter</Button>
        <Button size="xs" variant="ghost" onClick={() => setOpen((v) => !v)} ariaLabel="Why this job">{open ? '▴' : '▾'}</Button>
      </div>
      {!done && <Helper size="xs" style={{ paddingLeft: 38 }}>{j.reason}</Helper>}
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

function ContactCard({ c, job, onStatus, push }) {
  const done = CONTACT_DONE.has(c.status)
  return (
    <Card style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 14px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
        <div style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column', opacity: done ? DIM : 1 }}>
          <Link href={c.linkedin_url} target="_blank" rel="noreferrer">{c.name}</Link>
          <Helper size="xs" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.title || ''} · {c.company}{c.location ? ` · ${c.location}` : ''}</Helper>
          {job && <Helper size="xs" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>re #{job.rank} {job.title}</Helper>}
          {c.company_note && <Helper size="xs" title={c.company_note} style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.company_note}</Helper>}
        </div>
        {c.kind && <Tag style={{ opacity: done ? DIM : 1 }} tone={c.kind === 'hiring_manager' || c.kind === 'founder' ? 'accent' : 'neutral'}>{KIND[c.kind] || c.kind}</Tag>}
        <Select value={CONTACT_STATUS.some(([k]) => k === c.status) ? c.status : 'requested'} options={CONTACT_STATUS} onPick={(v) => onStatus(c, v)} width="120px" ariaLabel="Contact status" />
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', opacity: done ? DIM : 1 }}>
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
  const navigate = useNavigate()
  const { toasts, push, dismiss } = useToasts()
  const [sort, setSort] = useState('rank')
  const [day, setDay] = useState(tzDateKey())
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [running, setRunning] = useState(false)
  const [due, setDue] = useState(null)

  useEffect(() => {
    api.get('/contacts', { params: { due: true, limit: 200 } }).then(({ data }) => setDue((data || []).length)).catch(() => setDue(null))
  }, [])

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

  // ?job= picks the job, ?resume= the résumé it scored best against (same deep link as the Feed's `c`)
  const openCoverLetter = (j) => navigate(`/cover-letters?job=${j.id}${j.best_cv_id ? `&resume=${encodeURIComponent(j.best_cv_id)}` : ''}`)

  // one click: start tailoring from the best-fit base (General when Persona fit best), then show the Résumés tab,
  // which lists the run as "tailoring…" until the copy lands
  const tailorJob = async (j) => {
    try {
      await api.post('/resumes/tailor', { base_resume_id: j.tailor_base_id, job_id: j.id })
      push({ kind: 'progress', msg: `Tailoring for "${j.title}"…` })
    } catch (e) {
      if (e.response?.status !== 409) { push({ kind: 'error', msg: `Tailor failed for "${j.title}"${e.response?.data?.detail ? `: ${e.response.data.detail}` : ''}` }); return }
    }
    navigate('/resumes')
  }

  const allJobs = data?.jobs || []
  const allContacts = useMemo(() => (data?.contacts || []).filter((c) => !SCOUTED.has(c.lead_kind)), [data])
  const jobContacts = useMemo(() => allContacts.filter((c) => c.purpose !== 'network'), [allContacts])
  const network = useMemo(() => allContacts.filter((c) => c.purpose === 'network' && !CONTACT_DONE.has(c.status)), [allContacts])
  const applied = allJobs.filter((j) => j.status === 'applied').length
  const jobById = useMemo(() => Object.fromEntries(allJobs.map((j) => [j.id, j])), [allJobs])
  const sortedJobs = useMemo(() => [...allJobs].sort(SORT_BY[sort]), [allJobs, sort])
  const todo = useMemo(() => sortedJobs.filter((j) => j.status === 'saved'), [sortedJobs])
  const doneJobs = useMemo(() => sortedJobs.filter((j) => j.status !== 'saved'), [sortedJobs])
  // people follow the job list's order
  const pos = useMemo(() => Object.fromEntries(todo.concat(doneJobs).map((j, i) => [j.id, i])), [todo, doneJobs])
  const byJob = (a, b) => ((pos[a.job_id] ?? 1e9) - (pos[b.job_id] ?? 1e9))
  const contacts = useMemo(() => jobContacts.filter((c) => !CONTACT_DONE.has(c.status)).sort(byJob),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [jobContacts, pos])
  const doneContacts = useMemo(() => allContacts.filter((c) => CONTACT_DONE.has(c.status)).sort(byJob),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [allContacts, pos])
  const dayOpts = useMemo(() => {
    const ds = new Set([tzDateKey(), ...(data?.days || [])])
    return [...ds].sort().reverse().map((d) => [d, d === tzDateKey() ? `Today · ${d}` : d])
  }, [data])

  return (
    <div style={{ position: 'relative', flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 18 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
          <PageTitle>Today</PageTitle>
          <Helper>{allJobs.length ? `${allJobs.length} picks · ${applied} applied · ${allContacts.length} people · ranked in ${TZ_SHORT}` : 'The curator runs every morning and saves the best jobs here'}</Helper>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
          {due ? <Button variant="ghost" onClick={() => navigate('/people')} title="People whose next step is due">{due} network action{due === 1 ? '' : 's'} due</Button> : null}
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
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Heading strong={600} size={16}>{sort === 'rank' ? 'Apply in this order' : 'Jobs'}</Heading>
                {allJobs.length > 0 && (
                  <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
                    <Label size="xs">Sort</Label>
                    <Select value={sort} options={SORTS} onPick={setSort} width="120px" ariaLabel="Sort jobs" />
                  </div>
                )}
              </div>
              {allJobs.length === 0 && <Helper>No curated jobs for {day}. Run now, or wait for the morning run.</Helper>}
              {allJobs.length > 0 && todo.length === 0 && <Helper>All done for {day}.</Helper>}
              {todo.map((j) => <JobCard key={j.id} j={j} onStatus={setJobStatus} onCoverLetter={openCoverLetter} onTailor={tailorJob} />)}
              {doneJobs.length > 0 && (
                <>
                  <Heading strong={600} size={16} style={{ marginTop: 14 }}>Applied / skipped · {doneJobs.length}</Heading>
                  {doneJobs.map((j) => <JobCard key={j.id} j={j} onStatus={setJobStatus} onCoverLetter={openCoverLetter} onTailor={tailorJob} />)}
                </>
              )}
            </section>
            <section style={{ flex: '1 1 340px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <Heading strong={600} size={16}>People to contact</Heading>
              {jobContacts.length === 0 && <Helper>Nobody found for {day} yet.</Helper>}
              {contacts.map((c) => <ContactCard key={c.id} c={c} job={jobById[c.job_id]} onStatus={setContactStatus} push={push} />)}
              {network.length > 0 && (
                <>
                  <Heading strong={600} size={16} style={{ marginTop: 14 }}>People to know</Heading>
                  <Helper size="xs">Engineering leaders at good companies, open role or not. Ask for a chat, and a referral when a role opens.</Helper>
                  {network.map((c) => <ContactCard key={c.id} c={c} onStatus={setContactStatus} push={push} />)}
                </>
              )}
              {doneContacts.length > 0 && (
                <>
                  <Heading strong={600} size={16} style={{ marginTop: 14 }}>Requested / skipped · {doneContacts.length}</Heading>
                  {doneContacts.map((c) => <ContactCard key={c.id} c={c} job={jobById[c.job_id]} onStatus={setContactStatus} push={push} />)}
                </>
              )}
            </section>
          </>
        )}
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
