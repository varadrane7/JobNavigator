import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import ClosedMark from '../ClosedMark'
import { useParams, useNavigate } from 'react-router-dom'
import api from '../api'
import { track } from '../telemetry'
import { useFlushBeforeSwitch } from '../accounts'
import '../theme.css'
import { useToasts, ToastStack } from '../Toast'
import ConfirmDialog from '../ConfirmDialog'
import { downloadErrorReason, downloadName } from '../download'
import { useEscape, useSingleOpen, setFlashToast, fetchRunOutcome, runFailed, runFailureReason, useSettled, NBSP } from '../hooks'
import { useTitle } from '../useTitle'
import { loadDemo } from '../demoHooks'
// The résumé-content editors are shared with /persona (a Persona's
// resume_content is the same shape as a Resume's json_data).
import {
  EMPTY, resumeSectionNames, sectionCounts, makeMutators,
  SectionShell, SectionEditor, BandRule,
} from './ResumeSections'
import ResumeName from './ResumeName'
import { Band, Button, Check, ChoiceCard, ChoiceModal, ChoiceRow, FooterRow, Heading, HeaderRow, Helper, IconButton, Input, Label, Menu, MenuHead, MenuItem, ModalPanel, Mono, NavLink, Pill, Rule, ScoreRing, Spinner, Surface, Textarea, ToolbarTrigger } from '../ui'

// contiguous prefix/suffix word diff → { before, removed, added, after }
function wordDiff(a = '', b = '') {
  a = a || ''; b = b || ''
  if (a === b) return null
  const aw = a.split(' '), bw = b.split(' ')
  let p = 0
  while (p < aw.length && p < bw.length && aw[p] === bw[p]) p++
  let sa = aw.length, sb = bw.length
  while (sa > p && sb > p && aw[sa - 1] === bw[sb - 1]) { sa--; sb-- }
  const before = aw.slice(0, p).join(' ')
  return {
    before: before ? before + ' ' : '',
    removed: aw.slice(p, sa).join(' '),
    added: bw.slice(p, sb).join(' '),
    after: sa < aw.length ? ' ' + aw.slice(sa).join(' ') : '',
  }
}
// changes a tailored copy carries vs its base: modified summary/bullets + added (suggested) bullets
function computeChanges(base, copy) {
  if (!base || !copy) return []
  const out = []
  const sd = wordDiff(base.summary, copy.summary)
  if (sd) out.push({ key: 'summary', where: 'Summary', kind: 'modified', path: 'summary', baseText: base.summary || '', ...sd })
  ;(copy.experience || []).forEach((ce, i) => {
    const be = (base.experience || [])[i] || {}
    const bb = be.bullets || [], cb = ce.bullets || []
    cb.forEach((txt, j) => {
      if (j < bb.length) { const d = wordDiff(bb[j], txt); if (d) out.push({ key: `exp${i}b${j}`, where: `Experience · ${ce.company || 'role'} · bullet ${j + 1}`, kind: 'modified', path: `experience.${i}.bullets.${j}`, baseText: bb[j], ...d }) }
      else out.push({ key: `exp${i}nb${j}`, where: `Experience · ${ce.company || 'role'} · new bullet`, kind: 'added', before: '', removed: '', added: txt, after: '', text: txt })
    })
    ;(ce.suggested_bullets || []).forEach((sb, k) => out.push({ key: `exp${i}sb${k}`, where: `Experience · ${ce.company || 'role'} · suggested bullet`, kind: 'suggested', expIdx: i, sbIdx: k, before: '', removed: '', added: sb, after: '', text: sb }))
  })
  const bs = base.skills || {}, cs = copy.skills || {}
  Object.keys(cs).forEach((cat) => {
    if (!(cat in bs)) out.push({ key: `sk:${cat}`, where: `Skills · ${cat}`, kind: 'added', path: `skills.${cat}`, before: '', removed: '', added: String(cs[cat] || ''), after: '', text: String(cs[cat] || '') })
    else { const d = wordDiff(String(bs[cat] || ''), String(cs[cat] || '')); if (d) out.push({ key: `sk:${cat}`, where: `Skills · ${cat}`, kind: 'modified', path: `skills.${cat}`, baseText: String(bs[cat] || ''), ...d }) }
  })
  return out
}
const timeAgo = (s) => {
  if (!s) return ''
  const m = Math.floor((Date.now() - new Date(s).getTime()) / 60000)
  if (m < 1) return 'just now'; if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60); if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

// Shared field/section components live in ResumeSections.jsx so /persona can reuse them.
// Tracks tailoring-changes acknowledgement in localStorage (per-user UI state); promote to json_data if it must survive a browser change.
const REVIEWED_KEY = 'jobnavigator_v2_resume_reviewed'
const readReviewed = () => { try { const a = JSON.parse(localStorage.getItem(REVIEWED_KEY)); return Array.isArray(a) ? a : [] } catch { return [] } }
const markReviewed = (rid) => { try { localStorage.setItem(REVIEWED_KEY, JSON.stringify([...readReviewed().filter((x) => x !== rid), rid].slice(-300))) } catch { /* ignore */ } }

export default function ResumeEditor() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [doc, setDoc] = useState(null)
  const [data, setData] = useState(null)
  const [templates, setTemplates] = useState([])
  const [template, setTemplate] = useState('')
  const [format, setFormat] = useState('letter')
  const [savedAt, setSavedAt] = useState(null)
  const [saving, setSaving] = useState(false)
  const [pdfUrl, setPdfUrl] = useState(null)
  const [open, setOpen] = useState(() => {
    try { const s = JSON.parse(localStorage.getItem('jobnavigator_v2_resume_sections')); if (Array.isArray(s)) return new Set(s) } catch { /* ignore */ }
    return new Set(['Experience'])
  })
  const [tplOpen, setTplOpen] = useState(false)
  const [fmtOpen, setFmtOpen] = useState(false)
  const [reviewOpen, setReviewOpen] = useState(false)
  const [reviewed, setReviewed] = useState(false)   // tailoring changes acknowledged for this copy
  const [pdfErr, setPdfErr] = useState(false)      // last preview render failed
  const [pdfNonce, setPdfNonce] = useState(0)      // Retry re-arms the preview effect
  const [baseData, setBaseData] = useState(null)   // parent json_data (for diff + inline marks)
  const [jobData, setJobData] = useState(null)     // the copy's job (cv_scores, status)
  const [tracers, setTracers] = useState([])
  const [coverExists, setCoverExists] = useState(false)
  const [baseCopyCount, setBaseCopyCount] = useState(null)
  const [scoring, setScoring] = useState(false)
  const [headMenu, setHeadMenu] = useState(false)
  const [renaming, setRenaming] = useState(false)   // the top-bar name is an input
  const [confirm, setConfirm] = useState(null)   // v2 dialog, not window.confirm
  const [jobErr, setJobErr] = useState(false)   // the linked job failed to load
  const [parentName, setParentName] = useState(null)   // the base this copy came from
  const saveTimer = useRef(null)
  const pdfTimer = useRef(null)

  const isCopy = doc && !doc.is_base

  // The ⋯ head menu closes on backdrop click but needs Escape too, like every modal it can open.
  // Safe unconditionally: menu items call setHeadMenu(false) before opening a modal, so they never race for one keypress.
  useEscape(() => setHeadMenu(false), headMenu)

  const { toasts, push: pushToast, dismiss: dismissToast } = useToasts()

  // Closes both pickers on any outside click (each picker stops its own clicks) and on Escape.
  // Escape is marked handled so a modal opened over them isn't closed by the same keypress.
  useEffect(() => {
    if (!tplOpen && !fmtOpen) return undefined
    const close = () => { setTplOpen(false); setFmtOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape' && !e.defaultPrevented) { e.preventDefault(); close() } }
    document.addEventListener('click', close)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('click', close); document.removeEventListener('keydown', onKey) }
  }, [tplOpen, fmtOpen])
  // app-wide single-open: these two share one "close" already; broadcasting it
  // through the same registry Select uses means opening a Select elsewhere (or
  // this app's cron preset, or a Feed filter) closes these too, and vice versa.
  useSingleOpen(tplOpen || fmtOpen, () => { setTplOpen(false); setFmtOpen(false) })

  useEffect(() => { setReviewed(readReviewed().includes(id)) }, [id])

  useEffect(() => {
    let alive = true
    api.get(`/resumes/${id}`).then(({ data: d }) => {
      if (!alive) return
      setDoc(d); setData(d.json_data || EMPTY); setTemplate(d.template || ''); setFormat(d.page_format || 'letter'); setSavedAt(d.updated_at)
    }).catch((e) => {
      // A missing/deleted/malformed id lands on the shelf silently otherwise, indistinguishable from pressing "‹ Résumés".
      // This screen unmounts, so the message is handed to the shelf instead of pushed here.
      setFlashToast({ kind: 'error', msg: e.response?.status === 404 ? 'That résumé no longer exists.' : 'Couldn’t load that résumé.' })
      navigate('/resumes')
    })
    return () => { alive = false }
  }, [id, navigate])

  // The preview toolbar's pickers wait on this list so the Template trigger doesn't paint
  // the raw id then rename itself a moment later; pairs with the `!doc` gate below.
  const { ready: tplReady } = useSettled([
    () => api.get('/resumes/templates').then(({ data }) => setTemplates(data || [])).catch((e) => { console.error(e); pushToast({ kind: 'error', msg: 'Could not load the layouts — the picker is empty.' }) }),
  ])

  // The document these context loaders belong to — a response that arrives after
  // the user has moved to another résumé is dropped.
  const docIdRef = useRef(null)
  docIdRef.current = doc ? String(doc.id) : null

  const changes = useMemo(() => (isCopy && baseData && data ? computeChanges(baseData, data) : []), [isCopy, baseData, data])
  const changedSections = useMemo(() => {
    const s = new Set()
    changes.forEach((c) => { if (c.key === 'summary') s.add('Summary'); else if (c.key.startsWith('exp')) s.add('Experience'); else if (c.key.startsWith('sk:')) s.add('Skills') })
    return s
  }, [changes])

  // copy job context: score/delta, tracers, cover-letter existence
  const loadJobCtx = useCallback(() => {
    if (!doc || doc.is_base || !doc.job_id) return Promise.resolve()
    const mine = String(doc.id)
    return Promise.all([
      api.get(`/jobs/${doc.job_id}`).then(({ data: j }) => { if (docIdRef.current === mine) { setJobData(j); setJobErr(false) } }).catch(() => { if (docIdRef.current === mine) setJobErr(true) }),
      api.get(`/cover-letters`, { params: { job_id: doc.job_id } }).then(({ data }) => { if (docIdRef.current === mine) setCoverExists((data || []).length > 0) }).catch(() => { /* silent: only picks the wording of the cover-letter step */ }),
    ])
  }, [doc])

  // Everything the context band shows about a copy (fit ring, "Tailored for…" line,
  // base link, status/tracer line, next-step button) settles as one instead of rewriting itself twice.
  const { ready: ctxReady } = useSettled([
    // parent base data for the diff/marks (tailored copies only); copy count for a base
    () => {
      if (doc && doc.is_base) {
        const mine = String(doc.id)
        setBaseData(null)
        return api.get('/resumes', { params: { is_base: false } }).then(({ data }) => { if (docIdRef.current === mine) setBaseCopyCount((data || []).filter((r) => String(r.parent_id) === mine).length) }).catch(() => { /* silent: an auxiliary count in the band; absent it simply doesn't render */ })
      }
      if (doc && doc.parent_id) {
        const mine = String(doc.id)
        return api.get(`/resumes/${doc.parent_id}`).then(({ data: p }) => { if (docIdRef.current === mine) { setBaseData(p.json_data || null); setParentName(p.name || null) } }).catch(() => { /* silent: the diff marks and the base name degrade to the name heuristic */ })
      }
      setBaseData(null); setParentName(null)
      return null
    },
    () => {
      if (!doc || doc.is_base) { setJobData(null); setTracers([]); setJobErr(false); return null }
      if (!doc.job_id) { setJobData(null); setJobErr(false) }   // no job to load, so no failure to report
      const mine = String(doc.id)
      return Promise.all([
        loadJobCtx(),
        api.get(`/resumes/${id}/tracer-stats`).then(({ data }) => { if (docIdRef.current === mine) setTracers(data || []) }).catch(() => { /* silent: an optional click-count suffix on the band line */ }),
      ])
    },
  ], doc ? String(doc.id) : '')

  // A copy tailored from a pasted description has no Job row — the JD lives on the
  // copy (json_data._tailor_context) and so does its score (json_data._score), which makes it scoreable.
  const freeformJd = ((data && data._tailor_context && data._tailor_context.job_description) || '').trim()
  const jobless = !!doc && !doc.is_base && !doc.job_id

  const scores = useMemo(() => {
    if (doc && !doc.is_base && !doc.job_id) {
      const t = data && data._score && typeof data._score.Tailored === 'number' ? Math.round(data._score.Tailored) : null
      return { tailored: t, base: null, delta: null }
    }
    const cs = jobData?.cv_scores || {}
    const tailored = typeof cs['Tailored'] === 'number' ? Math.round(cs['Tailored']) : null
    const others = Object.entries(cs).filter(([k, v]) => k !== 'Tailored' && typeof v === 'number').map(([, v]) => v)
    const base = others.length ? Math.round(Math.max(...others)) : null
    return { tailored, base, delta: tailored != null && base != null ? tailored - base : null }
  }, [jobData, doc, data])

  const runScore = useCallback(async (depth) => {
    if (!doc?.job_id && !freeformJd) { pushToast({ kind: 'error', msg: 'This copy has no job and no saved description to score against.' }); return }
    setHeadMenu(false); setScoring(true)
    // Watches the run (scope `{job}:resume:{resume}`, routes_resumes.py) rather than
    // polling cv_scores.Tailored directly, which would match a re-score's stale value on the first tick.
    const scope = doc.job_id ? `${doc.job_id}:resume:${id}` : `resume:${id}`
    try {
      const { data: started } = await api.post(`/resumes/${id}/score-check`, { depth })
      track('score_run', { depth })
      const runId = started?.run_id || null   // identifies the run below
      pushToast({ kind: 'progress', msg: `Scoring (${depth}) — runs in the background.` })
      const t0 = Date.now()
      let seen = false
      const iv = setInterval(async () => {
        try {
          if (Date.now() - t0 > 180000) { clearInterval(iv); setScoring(false); return }   // a run that never ends
          const { data: runs } = await api.get('/monitor/active')
          const live = (runs || []).some((r) => r.job_type === 'score_resume' && r.scope_key === scope)
          if (live) { seen = true; return }
          if (!seen && Date.now() - t0 < 8000) return   // not started yet ≠ finished
          clearInterval(iv); setScoring(false)
          // Leaving /monitor/active isn't "it worked" — check run status before reading the score.
          // A failed re-score leaves the previous score on the job, which would otherwise read as a fresh success.
          const run = await fetchRunOutcome(runId, 'score_resume')
          if (runFailed(run)) { pushToast({ kind: 'error', msg: `Scoring failed — ${runFailureReason(run)}` }); return }
          if (!doc.job_id) {
            // the score of a job-less copy lands on the copy itself — merge just that
            // key back so edits made while it ran are not overwritten
            const { data: r } = await api.get(`/resumes/${id}`)
            const sc0 = r?.json_data?._score?.Tailored
            setData((prev) => ({ ...prev, _score: r?.json_data?._score }))
            if (typeof sc0 === 'number') pushToast({ kind: 'success', msg: `Scored: ${Math.round(sc0)}` })
            else pushToast({ kind: 'error', msg: 'Scoring finished without a score — try again.' })
            return
          }
          const { data: j } = await api.get(`/jobs/${doc.job_id}`)
          setJobData(j)
          const cs = j?.cv_scores || {}
          const sc = typeof cs['Tailored'] === 'number' ? Math.round(cs['Tailored']) : null
          const others = Object.entries(cs).filter(([k, v]) => k !== 'Tailored' && typeof v === 'number').map(([, v]) => v)
          const b = others.length ? Math.round(Math.max(...others)) : null
          if (sc == null) pushToast({ kind: 'error', msg: 'Scoring finished without a score — try again.' })
          else pushToast({ kind: 'success', msg: `Scored: ${sc}${b != null ? ` (${sc - b >= 0 ? '+' : ''}${sc - b} vs base)` : ''}` })
        } catch { if (Date.now() - t0 > 90000) { clearInterval(iv); setScoring(false) } }
      }, 3000)
    } catch (e) { setScoring(false); pushToast({ kind: 'error', msg: e.response?.status === 409 ? 'Already scoring this copy.' : (e.response?.data?.detail || 'Scoring failed to start.') }) }
  }, [doc, id, pushToast, freeformJd])

  const markApplied = useCallback(async () => {
    if (!doc?.job_id) return
    setHeadMenu(false)
    try { await api.patch(`/jobs/${doc.job_id}`, { status: 'applied' }); loadJobCtx(); pushToast({ kind: 'success', msg: 'Marked applied.' }) } catch { pushToast({ kind: 'error', msg: 'Could not mark applied.' }) }
  }, [doc, loadJobCtx, pushToast])

  const deleteResume = useCallback(() => {
    setHeadMenu(false)
    setConfirm({
      title: `Delete “${doc.name}”?`,
      body: doc.is_base ? 'Its tailored copies will be removed too.' : undefined,
      label: 'Delete', danger: true,
      onConfirm: async () => {
        setConfirm(null)
        try { await api.delete(`/resumes/${id}`); navigate('/resumes') } catch { pushToast({ kind: 'error', msg: 'Delete failed.' }) }
      },
    })
  }, [doc, id, navigate, pushToast])

  // Saves on its own (not through the debounced persist): `persist` keeps one pending patch,
  // so a rename sent through it would replace an unsaved json_data edit.
  // Both setDoc calls check the id: the request can end after the user opens another résumé.
  const rename = useCallback(async (value) => {
    setRenaming(false)
    const next = (value || '').trim()
    if (!next || next === doc.name) return
    const mine = String(doc.id), prev = doc.name
    const setName = (n) => setDoc((d) => (d && String(d.id) === mine ? { ...d, name: n } : d))
    setName(next)
    try { await api.patch(`/resumes/${mine}`, { name: next }); setSavedAt(new Date().toISOString()) }
    catch (e) { setName(prev); pushToast({ kind: 'error', msg: `Rename failed — ${e.response?.data?.detail || e.message || 'try again'}.` }) }
  }, [doc, pushToast])
  const startRename = () => { setHeadMenu(false); setRenaming(true) }

  const goCover = () => { setHeadMenu(false); navigate(`/cover-letters?resume=${id}${doc.job_id ? `&job=${doc.job_id}` : ''}`) }

  // the "one next step" stage for a tailored copy: Score is offered only when there's
  // something to score against; a copy with neither a job nor a saved description skips straight to the cover letter.
  const stage = useMemo(() => {
    if (!isCopy) return null
    if (changes.length && !reviewed) return { label: `Review ${changes.length} change${changes.length === 1 ? '' : 's'}`, act: () => setReviewOpen(true) }
    const scoreable = !!doc.job_id || !!freeformJd
    if (scoreable && scores.tailored == null) return { label: scoring ? 'Scoring…' : 'Score the result', act: () => runScore('full') }
    if (!coverExists) return { label: '✉ Write cover letter', act: goCover }
    if (!doc.job_id) return { label: 'Ready ✓', act: null, done: true }
    if (jobData?.status !== 'applied') return { label: 'Mark applied', act: markApplied }
    return { label: 'Applied ✓', act: null, done: true }
  }, [isCopy, doc, freeformJd, changes.length, reviewed, scores.tailored, scoring, coverExists, jobData, runScore, markApplied]) // eslint-disable-line

  // debounced persist
  // QA F7 / R6-01: the patch the timer still owes, so a switch can send it first
  const pendingSave = useRef(null)
  const sendSave = useCallback(async (patch) => {
    try { await api.patch(`/resumes/${id}`, patch); setSavedAt(new Date().toISOString()) }
    catch (e) { console.error(e); setSavedAt(null); pushToast({ kind: 'error', msg: `Save failed — your last edit is not stored. ${e.response?.data?.detail || e.message || ''}`.trim() }) }
    setSaving(false)
  }, [id])  // eslint-disable-line react-hooks/exhaustive-deps
  const persist = useCallback((patch) => {
    setSaving(true)
    clearTimeout(saveTimer.current)
    pendingSave.current = patch
    saveTimer.current = setTimeout(() => { pendingSave.current = null; sendSave(patch) }, 500)
  }, [sendSave])
  // Sends the patch the timer still owes now (a switch, a download); undefined when none.
  const flushSave = useCallback(() => {
    const patch = pendingSave.current
    if (!patch) return undefined
    clearTimeout(saveTimer.current); pendingSave.current = null
    return sendSave(patch)
  }, [sendSave])
  useFlushBeforeSwitch(flushSave)
  const onData = useCallback((next) => { setData(next); persist({ json_data: next }) }, [persist])
  const pickTemplate = (t) => { setTemplate(t); setTplOpen(false); persist({ template: t }) }
  const pickFormat = (f) => { setFormat(f); setFmtOpen(false); persist({ page_format: f }) }

  // live PDF preview — one request at a time (abort the in-flight render before the
  // next so overlapping edits don't hammer /pdf or race the tracer-link writer)
  const pdfAbort = useRef(null)
  useEffect(() => {
    if (!doc) return
    clearTimeout(pdfTimer.current)
    pdfTimer.current = setTimeout(async () => {
      if (pdfAbort.current) pdfAbort.current.abort()
      const ac = new AbortController(); pdfAbort.current = ac
      try {
        const r = await api.get(`/resumes/${id}/pdf`, { responseType: 'arraybuffer', params: { template, format }, signal: ac.signal })
        const url = URL.createObjectURL(new Blob([r.data], { type: __DEMO__ ? 'text/html' : 'application/pdf' }))
        setPdfUrl((prev) => { if (prev) URL.revokeObjectURL(prev); return url })
        setPdfErr(false)
      } catch (e) {
        if (e.code === 'ERR_CANCELED' || e.name === 'CanceledError') return
        // Clears the preview on failure instead of leaving a stale PDF that looks current.
        console.error('pdf', e)
        setPdfUrl((prev) => { if (prev) URL.revokeObjectURL(prev); return null })
        setPdfErr(true)
      }
    }, 800)
    return () => clearTimeout(pdfTimer.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, template, format, doc, id, pdfNonce])
  useEffect(() => () => { if (pdfUrl) URL.revokeObjectURL(pdfUrl) }, [pdfUrl])

  // apply the decline-based review: declined modified/added → revert to base; kept
  // suggested bullets → append; then clear all suggested_bullets. One save.
  const applyReview = useCallback((declined) => {
    const d = JSON.parse(JSON.stringify(data || EMPTY))
    changes.forEach((c) => {
      const off = !!declined[c.key]
      if (c.kind === 'modified' && off) {
        const keys = c.path.split('.'); let o = d
        for (let i = 0; i < keys.length - 1; i++) o = o?.[keys[i]]
        if (o) o[keys[keys.length - 1]] = c.baseText
      } else if (c.kind === 'added' && off) {
        // remove the added copy bullet (match by text within its experience)
        d.experience?.forEach((e) => { if (e.bullets) e.bullets = e.bullets.filter((b) => b !== c.text) })
      } else if (c.kind === 'suggested' && !off) {
        const e = d.experience?.[c.expIdx]; if (e) { e.bullets = e.bullets || []; e.bullets.push(c.text) }
      }
    })
    ;(d.experience || []).forEach((e) => { delete e.suggested_bullets })
    onData(d)
    setReviewOpen(false)
    markReviewed(id); setReviewed(true)   // the diff survives; the acknowledgement is what advances the stage
    pushToast({ kind: 'success', msg: 'Review applied — declined changes restored to base.' })
  }, [changes, data, id, onData, pushToast])

  // ── json_data mutation (mirrors v1 ResumeContentEditor) ────────────────────
  useTitle(doc?.name)
  const { mutate, setField } = makeMutators(data, onData)
  const toggle = (name) => setOpen((p) => {
    const n = new Set(p); n.has(name) ? n.delete(name) : n.add(name)
    try { localStorage.setItem('jobnavigator_v2_resume_sections', JSON.stringify([...n])) } catch { /* ignore */ }
    return n
  })

  // Reserves the chrome's shape while the document loads, instead of a bare "Loading…"
  // that collapses the screen then jumps to the real layout once the doc lands.
  if (!doc || !data) {
    return (
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <HeaderRow pad="10px 24px" bg="surface" soft align="center">
          <span style={{ fontSize: 14, lineHeight: '20px' }}>{NBSP}</span>
        </HeaderRow>
        <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
          <section style={{ flex: '0 0 47%', borderRight: '1px solid var(--line)' }} />
          <Surface as="section" radius="none" style={{ flex: 1, minWidth: 0 }} />
        </div>
      </div>
    )
  }

  const counts = sectionCounts(data)
  const sectionNames = resumeSectionNames(doc)
  const tplLabel = templates.find((t) => t.id === template)?.name || template || 'Template'

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      {/* top bar */}
      <HeaderRow pad="10px 24px" bg="surface" soft align="center">
        <NavLink onClick={() => navigate('/resumes')} style={{ whiteSpace: 'nowrap' }}>‹ Résumés</NavLink>
        <span style={{ color: 'var(--line)' }}>|</span>
        {/* ui: keep — Tag-role uppercase badge (bg + r99, not a Label); case/tracking read --label-case/--label-tracking-scale so an alt skin can turn both off */}
        <span style={{ fontSize: 9.5, letterSpacing: 'calc(.08em * var(--label-tracking-scale))', textTransform: 'var(--label-case)', padding: '2px 7px', borderRadius: 'var(--radius-control)', background: isCopy ? 'var(--ai-soft)' : 'var(--surface-2)', color: isCopy ? 'var(--ai)' : 'var(--muted)' }}>{isCopy ? 'tailored' : 'base'}</span>
        <ResumeName name={doc.name} editing={renaming} onEdit={() => setRenaming(true)} onDone={rename} />
        <Helper style={{ marginLeft: 'auto' }}>{saving ? 'Saving…' : savedAt ? `saved ${timeAgo(savedAt)} · autosaves` : 'autosaves'}</Helper>
      </HeaderRow>

      {/* sub-band: base vs copy */}
      {isCopy ? (
        <HeaderRow pad="9px 24px" bg="recessed" align="center" style={{ gap: 13 }}>
          {ctxReady && scores.tailored != null && (
            <ScoreRing value={scores.tailored} size="sm" />
          )}
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
            {/* Both lines keep their boxes (18px + Helper's own) while the four context requests
                are in flight, so the band is at final height from the first frame. */}
            <div style={{ fontSize: 12.5, fontWeight: 500, color: 'var(--text-2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0, lineHeight: '18px' }}>
              {!ctxReady ? NBSP : <>
              {jobData?.company
                ? <>Tailored for <span style={{ color: 'var(--text)' }}>{jobData.company}{jobData.title ? ` — ${jobData.title}` : ''}</span><ClosedMark jobId={doc.job_id} /></>
                : (jobless && freeformJd ? 'Tailored from a pasted description' : 'Tailored copy')}
              {doc.parent_id && (() => {
                // A freeform copy has no "→" to split on, so the name heuristic returns the
                // copy's own name; prefer the fetched parent name and use the split only until it lands.
                const baseName = parentName || ((doc.name || '').includes('→') ? (doc.name || '').split('→')[0].trim() : '') || 'base'
                const dfg = scores.delta == null ? undefined : (scores.delta >= 0 ? 'var(--accent)' : 'var(--warn)')
                return (
                  <>
                    <BandRule />
                    <NavLink onClick={() => navigate(`/resumes/${doc.parent_id}`)} title={`Open the ${baseName} base résumé this was tailored from`} style={{ position: 'relative', top: '1px' }}>
                      {scores.delta != null && <span style={{ color: dfg, fontWeight: 600 }}>{scores.delta >= 0 ? '+' : ''}{scores.delta} </span>}
                      <span style={{ color: 'var(--accent)' }}>based on {baseName} ↗</span>
                    </NavLink>
                  </>
                )
              })()}
              </>}
            </div>
            <Helper style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {/* Distinguishes "not scored yet" from "can't be scored" and "job failed to load".
                  Tracer counts are part of the same settle, so the line doesn't grow a tail after the fact. */}
              {!ctxReady ? NBSP : <>
              {jobErr ? 'Couldn’t load the linked job.'
                : jobless && !freeformJd ? 'No job or description linked, so this copy can’t be scored.'
                  : (changes.length && !reviewed ? `${changes.length} reviewable change${changes.length === 1 ? '' : 's'}` : scores.tailored == null ? 'not scored yet' : 'ready')}
              {tracers.length > 0 && <> · tracked links: {tracers.map((t) => `${t.source_label} ${t.clicks}`).join(' · ')}</>}
              </>}
            </Helper>
          </div>
          {ctxReady && stage && (
            <Button onClick={() => stage.act && stage.act()} disabled={stage.done} title={stage.done ? 'All steps done' : 'Next step'}>
              {scoring && <Spinner size={11} color="currentColor" />}
              {stage.label}
            </Button>
          )}
          <div style={{ position: 'relative', flex: '0 0 auto' }}>
            <IconButton size={36} on={headMenu} ariaExpanded={headMenu} ariaHaspopup="menu"
              onClick={() => setHeadMenu((v) => !v)} title="More">⋯</IconButton>
            {headMenu && (
              <>
                <div onClick={() => setHeadMenu(false)} style={{ position: 'fixed', inset: 0, zIndex: 44 }} />
                <Menu ariaLabel="Résumé actions" style={{ position: 'absolute', top: '100%', right: 0, marginTop: 5, zIndex: 45, width: 244 }}>
                  <MenuHead>This copy</MenuHead>
                  <MenuItem icon="◎" hint="score only" onClick={() => runScore('light')}>Score again · light</MenuItem>
                  <MenuItem icon="◎" hint="with report" onClick={() => runScore('full')}>Score again · full</MenuItem>
                  {changes.length > 0 && <MenuItem icon="≋" hint={`${changes.length} to review`} onClick={() => { setHeadMenu(false); setReviewOpen(true) }}>Review changes</MenuItem>}
                  <MenuItem icon="✎" onClick={startRename}>Rename</MenuItem>
                  <Rule style={{ margin: '4px 8px' }} />
                  <MenuHead>Job</MenuHead>
                  {/* `hint` slots deliberately empty: they read as single-letter
                      shortcuts (the Feed really binds f/j/s/x/e/o), and nothing on
                      this screen implements c/e/a — its only key handlers are
                      Escape and ⌘B inside a bullet (R4-T2B-05). Binding them here
                      is not a one-liner: the editor is a page of text fields, so a
                      bare letter key needs a target/isContentEditable guard first. */}
                  <MenuItem icon="✉" onClick={goCover}>Cover letter</MenuItem>
                  {doc.job_id && <MenuItem icon="↗" onClick={() => navigate(`/feed?job=${doc.job_id}`)}>Open in feed</MenuItem>}
                  {doc.job_id && <MenuItem icon="✓" onClick={markApplied}>Mark applied</MenuItem>}
                  <MenuItem danger icon="✕" onClick={deleteResume}>Delete copy</MenuItem>
                </Menu>
              </>
            )}
          </div>
        </HeaderRow>
      ) : (
        <HeaderRow pad="9px 24px" bg="recessed" align="center" style={{ gap: 13, fontSize: 12.5, color: 'var(--text-2)' }}>
          {/* The copy count is part of the context settle; the whole sentence waits on ctxReady
              (not just the count) so it paints once instead of inserting the clause later. */}
          <span>{!ctxReady ? NBSP : <>Base résumé · {baseCopyCount != null && <><span style={{ color: 'var(--text)', fontWeight: 500 }}>{baseCopyCount} tailored cop{baseCopyCount === 1 ? 'y' : 'ies'}</span> · </>}edits here affect new copies only</>}</span>
          {/* Bases get the same ⋯ → Delete as copies (the confirm dialog already warns copies go too).
              Worded "Delete résumé" here since this document is the base and deleting it takes every copy with it. */}
          <div style={{ position: 'relative', flex: '0 0 auto', marginLeft: 8 }}>
            <IconButton size={36} on={headMenu} ariaExpanded={headMenu} ariaHaspopup="menu"
              onClick={() => setHeadMenu((v) => !v)} title="More">⋯</IconButton>
            {headMenu && (
              <>
                <div onClick={() => setHeadMenu(false)} style={{ position: 'fixed', inset: 0, zIndex: 44 }} />
                <Menu ariaLabel="Résumé actions" style={{ position: 'absolute', top: '100%', right: 0, marginTop: 5, zIndex: 45, width: 244 }}>
                  <MenuHead>This base</MenuHead>
                  <MenuItem icon="✎" onClick={startRename}>Rename</MenuItem>
                  <MenuItem danger icon="✕" onClick={deleteResume}>Delete résumé</MenuItem>
                </Menu>
              </>
            )}
          </div>
        </HeaderRow>
      )}

      {/* two-pane */}
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>
        {/* left: sections */}
        <section className="v2-scroll" style={{ flex: '0 0 47%', borderRight: '1px solid var(--line)', overflow: 'auto', padding: '14px 20px 24px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {sectionNames.map((name) => (
            <SectionShell key={name} name={name} count={counts[name]} open={open.has(name)} onToggle={() => toggle(name)}
              meta={changedSections.has(name) ? <span title="Contains unreviewed tailoring changes" style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--warn)' }}>● changed by tailoring</span> : null}>
              <SectionEditor name={name} data={data} setField={setField} mutate={mutate} baseData={baseData} onError={(msg) => pushToast({ kind: 'error', msg })}
                onRemoved={(msg, undo) => pushToast({ kind: 'undo', msg, action: 'Undo', onAction: undo })} />
            </SectionShell>
          ))}
        </section>

        {/* right: PDF preview */}
        <Surface as="section" radius="none" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          {/* wraps rather than overflowing, like the cover-letter editor's identical toolbar */}
          <HeaderRow pad="8px 20px" align="center" style={{ flexWrap: 'wrap', rowGap: 6 }}>
            <Label>PDF preview</Label>
            {/* template picker — the container swallows its own clicks so the document closer below can't undo the toggle */}
            {/* ui: keep — the two 9px muted ▾ carets below are the PDF-preview toolbar's own paper scale (below Helper's tolerance) */}
            {/* both triggers wait for the template list; the row's height is the Download link's, so nothing moves when they land */}
            {tplReady && <div style={{ position: 'relative' }} onClick={(e) => e.stopPropagation()}>
              <ToolbarTrigger label="Template" value={tplLabel} line="inherit" title="Résumé template"
                onClick={() => { setTplOpen((v) => !v); setFmtOpen(false) }} />
              {tplOpen && (
                  <Menu role="listbox" ariaLabel="Résumé template" className="v2-scroll" style={{ position: 'absolute', top: '100%', left: 0, marginTop: 5, zIndex: 21, width: 190, maxHeight: 300, overflow: 'auto' }}>
                    {templates.map((t) => <MenuItem key={t.id} role="option" ariaSelected={t.id === template} selected={t.id === template} onClick={() => pickTemplate(t.id)}>{t.name}</MenuItem>)}
                  </Menu>
              )}
            </div>}
            {/* format */}
            {tplReady && <div style={{ position: 'relative' }} onClick={(e) => e.stopPropagation()}>
              <ToolbarTrigger label="Paper" value={format === 'a4' ? 'A4' : 'US Letter'} line="inherit" title="Paper size"
                onClick={() => { setFmtOpen((v) => !v); setTplOpen(false) }} />
              {fmtOpen && (
                  <Menu role="listbox" ariaLabel="Paper size" style={{ position: 'absolute', top: '100%', left: 0, marginTop: 5, zIndex: 21, width: 130 }}>
                    {[['letter', 'US Letter'], ['a4', 'A4']].map(([v, l]) => <MenuItem key={v} role="option" ariaSelected={v === format} selected={v === format} onClick={() => pickFormat(v)}>{l}</MenuItem>)}
                  </Menu>
              )}
            </div>}
            <DownloadMenu id={id} template={template} format={format} beforeDownload={flushSave} onError={(msg) => pushToast({ kind: 'error', msg })} />
          </HeaderRow>
          <Surface radius="none" style={{ flex: 1, minHeight: 0, position: 'relative' }}>
            {pdfUrl && <iframe title="pdf" src={`${pdfUrl}#view=FitH`} style={{ width: '100%', height: '100%', border: 'none' }} />}
            {pdfErr && !pdfUrl && (
              <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 9, color: 'var(--muted)', fontSize: 12.5 }}>
                <span>Preview failed — the PDF could not be rendered.</span>
                <Pill size="sm" onClick={() => setPdfNonce((n) => n + 1)}>Retry</Pill>
              </div>
            )}
          </Surface>
        </Surface>
      </div>

      {reviewOpen && <ReviewModal changes={changes} onClose={() => setReviewOpen(false)} onApply={applyReview} />}
      {confirm && <ConfirmDialog {...confirm} onCancel={() => setConfirm(null)} />}

      <ToastStack toasts={toasts} onClose={dismissToast} />
    </div>
  )
}

// ── Download (PDF / Word) ────────────────────────────────────────────────────
// The preview toolbar's Download button and its menu; the Word export is based on work by
// @mrAlishah. Each choice goes through axios and saves the blob, like the cover-letter
// download, so the X-API-Key header travels and a failed export is an error toast rather
// than a JSON page in a new tab. The current template and paper ride along, so a fresh
// pick that the debounced save hasn't stored yet is still what downloads. `beforeDownload`
// sends an edit the debounced save still owes, so the file has the last keystroke (R30 D-06).
export function DownloadMenu({ id, template, format, onError, beforeDownload }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  useEscape(() => setOpen(false), open)
  const download = async (kind) => {
    setOpen(false); setBusy(true)
    try {
      await beforeDownload?.()
      const r = await api.get(`/resumes/${id}/${kind}`, { responseType: 'blob', params: { template, format } })
      track('resume_exported', { format: kind, template })
      if (__DEMO__ && kind === 'pdf') {   // the demo prints the rendered template through the browser
        const { printHtml, blobText } = await loadDemo('pdf.js')
        await printHtml(await blobText(r.data)); return
      }
      const url = URL.createObjectURL(r.data)
      const a = document.createElement('a')
      a.href = url; a.download = downloadName(r.headers?.['content-disposition'], `Resume.${kind}`)
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      console.error(e)
      const reason = await downloadErrorReason(e)
      const what = { docx: 'the Word document', tex: 'the LaTeX source' }[kind] || 'the PDF'
      onError?.(`Could not download ${what}` + (reason ? ` — ${reason}` : '.'))
    } finally { setBusy(false) }
  }
  // Overrides keep the toolbar's own 29px height and 12px label.
  return (
    <div style={{ position: 'relative', flex: '0 0 auto', marginLeft: 'auto' }}>
      <Button size="sm" busy={busy} ariaHaspopup="menu" ariaExpanded={open} onClick={() => setOpen((v) => !v)}
        style={{ minWidth: 0, height: 29, gap: 6, fontSize: 12 }}>↓ Download</Button>
      {open && (
        <Menu ariaLabel="Download résumé" onDismiss={() => setOpen(false)}
          style={{ position: 'absolute', top: '100%', right: 0, marginTop: 5, zIndex: 45, width: 168 }}>
          <MenuItem hint=".pdf" hintMono onClick={() => download('pdf')}>PDF</MenuItem>
          <MenuItem hint=".docx" hintMono onClick={() => download('docx')}>Word</MenuItem>
          <MenuItem hint=".tex" hintMono onClick={() => download('tex')}>LaTeX</MenuItem>
        </Menu>
      )}
    </div>
  )
}

// ── review modal (decline-based) ─────────────────────────────────────────────
function ReviewModal({ changes, onClose, onApply }) {
  const [declined, setDeclined] = useState({})
  const n = Object.values(declined).filter(Boolean).length
  // A modified summary/bullet is already in json_data (and the PDF) once the tailor finishes;
  // a *suggested* bullet lives in experience[].suggested_bullets and only reaches json_data when this modal is confirmed (applyReview).
  const nSuggested = changes.filter((c) => c.kind === 'suggested').length
  const nApplied = changes.length - nSuggested
  const liveApplied = changes.filter((c) => c.kind !== 'suggested' && !declined[c.key]).length
  const liveSuggested = changes.filter((c) => c.kind === 'suggested' && !declined[c.key]).length
  return (
    <ModalPanel width="min(920px, 94vw)" title={nSuggested ? 'Tailoring changes' : 'Tailoring changes — already applied'} onClose={onClose} zIndex={60} style={{ height: 'min(760px, 90vh)', overflow: 'hidden' }}>
        <HeaderRow align="center" style={{ gap: 10 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <Heading className="v2-dialogtitle">{nSuggested ? 'Tailoring changes' : 'Tailoring changes — already applied'}</Heading>
            <Helper>
              {nSuggested
                ? `${nApplied ? 'Applied changes are already in the document — decline any and the base text comes back. ' : ''}Suggested bullets are not in it yet: they are added when you finish reviewing.`
                : "These changes were applied automatically. Decline any you don't want to restore the base text."}
            </Helper>
          </div>
          <IconButton onClick={onClose} title="Close" style={{ marginLeft: 'auto' }}>✕</IconButton>
        </HeaderRow>
        <div className="v2-scroll" style={{ flex: 1, overflow: 'auto', padding: '15px 22px', display: 'flex', flexDirection: 'column', gap: 11, minHeight: 0 }}>
          {changes.length === 0 && <div style={{ padding: 20, fontSize: 12.5, color: 'var(--muted)' }}>No tailoring changes to review.</div>}
          {changes.map((c) => {
            const off = !!declined[c.key]
            const pending = c.kind === 'suggested'   // not in json_data yet
            const live = !off
            const added = c.kind === 'modified' ? (off ? c.removed : c.added) : c.added
            const removed = c.kind === 'modified' ? (off ? c.added : c.removed) : ''
            return (
              <div key={c.key} style={{ border: `1px solid ${!live ? 'var(--line)' : pending ? 'var(--warn-line)' : 'var(--change-soft)'}`, borderRadius: 'var(--radius-card)', padding: '11px 13px', display: 'flex', flexDirection: 'column', gap: 7, background: !live ? 'var(--bg)' : pending ? 'var(--warn-soft)' : 'var(--change-bg)', boxShadow: live && !pending ? 'var(--change-edge)' : undefined }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <Label>{c.where}</Label>
                  {/* ui: keep — state badge; dashed --warn-line edge marks a suggestion, not a dashed add-line */}
                  <span title={pending ? 'Suggested — not in the document or the PDF yet; added when you finish reviewing' : off ? 'Declined — the base text is restored' : 'Already in the document and in the PDF'}
                    style={{ fontSize: 10, lineHeight: '16px', letterSpacing: '.08em', textTransform: 'uppercase', padding: '1px 7px', borderRadius: 'var(--radius-control)', background: !live ? 'var(--surface-2)' : pending ? 'var(--surface)' : 'var(--accent-soft)', border: `1px ${live && pending ? 'dashed var(--warn-line)' : 'solid transparent'}`, color: !live ? 'var(--muted)' : pending ? 'var(--warn)' : 'var(--accent)', cursor: 'help' }}>{!live ? (pending ? 'dropped' : 'declined') : pending ? 'suggested' : 'applied'}</span>
                  {live && pending && <Helper size="xs">added when you finish reviewing</Helper>}
                  {/* ui: keep — border+ink swing --accent/--warn with the change's state; Pill has no tinted variant */}
                  <div onClick={() => setDeclined((p) => ({ ...p, [c.key]: !p[c.key] }))} style={{ marginLeft: 'auto', height: 24, padding: '0 12px', borderRadius: 'var(--radius-control)', border: `1px solid ${off ? 'var(--accent)' : 'var(--warn)'}`, color: off ? 'var(--accent)' : 'var(--warn)', fontSize: 11, fontWeight: 500, display: 'flex', alignItems: 'center', cursor: 'pointer' }}>{pending ? (off ? 'Keep it' : 'Drop ↩') : (off ? 'Restore change' : 'Decline ↩')}</div>
                </div>
                <span style={{ fontSize: 12.5, lineHeight: '20px', color: 'var(--text-2)' }}>
                  {c.before}
                  {removed && <span style={{ background: 'var(--bad-soft)', textDecoration: 'line-through', opacity: 0.75, borderRadius: 'var(--radius-mark)', padding: '0 3px' }}>{removed}</span>}
                  {/* ui: keep — inline diff highlight on a run of text (r3), not a band */}
                  {added && <span style={{ background: !live ? 'var(--surface-2)' : pending ? 'var(--surface)' : 'var(--change-soft)', border: `1px ${live && pending ? 'dashed var(--warn-line)' : 'solid transparent'}`, borderRadius: 'var(--radius-mark)', padding: '0 3px' }}>{added}</span>}
                  {c.after}
                </span>
              </div>
            )
          })}
        </div>
        <FooterRow bg="recessed">
          {/* the count line separates what is already in the document from what's only proposed */}
          <Helper>{nSuggested
            ? `${liveApplied} applied · ${liveSuggested} suggested — added on Done reviewing${nApplied - liveApplied ? ` · ${nApplied - liveApplied} declined` : ''}`
            : n ? `${n} declined and restored to the base text · the other changes are kept` : `All ${changes.length} change${changes.length === 1 ? '' : 's'} applied · decline one to restore its base text`}</Helper>
          <Button size="sm" onClick={() => onApply(declined)} style={{ marginLeft: 'auto' }}>Done reviewing</Button>
        </FooterRow>
    </ModalPanel>
  )
}
