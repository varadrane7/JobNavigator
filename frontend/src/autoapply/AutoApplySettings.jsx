import React, { useEffect, useMemo, useState } from 'react'
import api from '../api'
import { Button, Check, Drawer, Heading, Helper, Input, Label, Segmented, Select, Switch, Textarea } from '../ui'
import { getSettings, putSettings, getLlmUsage, errText } from './api'
import { LlmChain, toSlots, fromSlots } from './LlmChain'

// Sections in display order. Each field: [key, label, editor?]. The editor defaults to the
// setting's kind from the API; 'list' edits a JSON string array as one entry per line,
// 'resumes' picks base résumés, 'long' is a tall textarea, 'chain' is the priority 1-3 provider/model
// pickers, 'agentModel' picks the applier's Claude model.
const SECTIONS = [
  ['Run', [
    ['autoapply_enabled', 'Enabled'],
    ['autoapply_dry_run', 'Dry run (fill forms, never click submit)'],
    ['autoapply_mode', 'Submit gate'],
    ['autoapply_engine', 'Form filler'],
  ]],
  ['Daily queue', [
    ['autoapply_daily_cap', 'Daily cap'],
    ['autoapply_min_score', 'Min fit score (0-100)'],
    ['autoapply_max_age_days', 'Max job age (days)'],
    ['autoapply_score_budget', 'Scoring budget per build'],
    ['autoapply_company_daily_cap', 'Per-company daily cap'],
    ['autoapply_queue_cron', 'Queue build schedule (cron)'],
    ['autoapply_resume_ids', 'Base résumés to apply with', 'resumes'],
  ]],
  ['Filters', [
    ['autoapply_enabled_submitters', 'ATS kinds allowed', 'list'],
    ['autoapply_extra_aggregators', 'Extra job boards to ignore (domains)', 'list'],
    ['autoapply_company_blocklist', 'Company blocklist', 'list'],
    ['autoapply_title_blocklist', 'Title words to skip', 'list'],
    ['autoapply_site_caps', 'Per-ATS daily caps (JSON)'],
  ]],
  ['Pacing', [
    ['autoapply_jitter_min_s', 'Gap between submits, min (s)'],
    ['autoapply_jitter_max_s', 'Gap between submits, max (s)'],
    ['autoapply_quiet_hours', 'Quiet hours (start-end)'],
    ['autoapply_timezone', 'Time zone'],
  ]],
  ['Agent (applier service)', [
    ['autoapply_applier_url', 'Applier URL'],
    ['autoapply_agent_model', 'Claude model', 'agentModel'],
    ['autoapply_agent_timeout_s', 'Time limit per application (s)'],
    ['autoapply_agent_max_turns', 'Max agent turns'],
    ['autoapply_agent_autofill', 'Use the Navigator extension + draft_answer (saves model turns)'],
    ['autoapply_agent_backend_url', 'Backend URL as seen from the applier'],
    ['autoapply_agent_instructions', 'Extra instructions for every application', 'long'],
    ['autoapply_live_view_url', 'Live view link'],
  ]],
  ['Documents', [
    ['autoapply_cover_letter', 'Cover letter'],
    ['autoapply_allow_new_skills', 'Allow new skills when tailoring'],
    ['autoapply_tailor_prompt', 'Tailoring prompt (summary + skills only)', 'long'],
  ]],
  ['LLM routing (tailoring, answers)', [
    ['autoapply_llm_cheap', 'Cheap tier — form answers (high volume)', 'chain'],
    ['autoapply_llm_quality', 'Quality tier — tailoring, cover letters, free-text answers', 'chain'],
    ['autoapply_llm_quota_cooldown_min', 'Skip a provider after a quota hit (min)'],
    ['autoapply_llm_error_cooldown_min', 'Skip a provider after an error (min)'],
  ]],
  ['Built-in filler only', [
    ['autoapply_submit_timeout_s', 'Per-application timeout (s)'],
    ['autoapply_site_pause_hours', 'Pause a site after a checkpoint (h)'],
  ]],
]
const ALL = SECTIONS.flatMap(([, f]) => f)

// The applier runs Claude Code, so it takes Claude Code's aliases or any claude_code catalog id.
const AGENT_ALIASES = [['', 'applier default (APPLIER_MODEL, sonnet)'], ['haiku', 'haiku — lightest, may miss tricky forms'],
  ['sonnet', 'sonnet — balanced'], ['opus', 'opus — heaviest on limits']]

// Stored value -> what the editor holds, and back. Throws a readable error on bad input.
const toDraft = (editor, kind, v) => {
  if (editor === 'list') return (v || []).join('\n')
  if (editor === 'resumes') return (v || []).map(String)
  if (editor === 'chain') return toSlots(v)
  if (kind === 'json') return JSON.stringify(v, null, 1)
  if (kind === 'int') return v == null ? '' : String(v)
  return v
}
const fromDraft = (editor, kind, d, label) => {
  if (editor === 'list') return String(d || '').split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
  if (editor === 'resumes') return d
  if (editor === 'chain') return fromSlots(d)
  if (kind === 'json') {
    try { return JSON.parse(d || 'null') } catch { throw new Error(`${label}: not valid JSON`) }
  }
  if (kind === 'int') {
    if (!/^\d+$/.test(String(d).trim())) throw new Error(`${label}: needs a whole number`)
    return parseInt(d, 10)
  }
  return d
}

export default function AutoApplySettings({ onClose, onSaved }) {
  const [spec, setSpec] = useState(null)
  const [orig, setOrig] = useState({})
  const [draft, setDraft] = useState({})
  const [resumes, setResumes] = useState([])
  const [catalog, setCatalog] = useState([])   // main Settings' llm_models_list
  const [efforts, setEfforts] = useState({})
  const [usage, setUsage] = useState({ usage: [], cooling: {} })
  const [err, setErr] = useState('')
  const [fieldErr, setFieldErr] = useState({})
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    getSettings().then((d) => {
      setSpec(d.spec)
      const v = {}
      for (const [k, , ed] of ALL) v[k] = toDraft(ed, d.spec[k]?.kind, d.values[k])
      setOrig(v); setDraft(v)
    }).catch((e) => setErr(errText(e, 'Could not load settings')))
    api.get('/resumes', { params: { is_base: true } }).then(({ data }) => setResumes(data || [])).catch(() => {})
    api.get('/settings').then(({ data }) => {
      let m = data?.llm_models_list
      if (typeof m === 'string') { try { m = JSON.parse(m) } catch { m = [] } }
      setCatalog(Array.isArray(m) ? m : [])
    }).catch(() => {})
    api.get('/llm/efforts').then(({ data }) => setEfforts(data?.efforts || {})).catch(() => {})
    getLlmUsage(7).then((d) => setUsage({ usage: d.usage || [], cooling: d.cooling || {} })).catch(() => {})
  }, [])

  const dirty = useMemo(() => ALL.filter(([k]) => JSON.stringify(draft[k]) !== JSON.stringify(orig[k])).map(([k]) => k), [draft, orig])
  const set = (k) => (val) => { setDraft((p) => ({ ...p, [k]: val })); setFieldErr((p) => ({ ...p, [k]: undefined })) }

  const save = async () => {
    setBusy(true); setErr(''); setFieldErr({})
    try {
      const out = {}
      for (const [k, label, ed] of ALL) {
        if (!dirty.includes(k)) continue
        try { out[k] = fromDraft(ed, spec[k]?.kind, draft[k], label) } catch (e) { setFieldErr({ [k]: e.message }); throw e }
      }
      if (Object.keys(out).length) await putSettings(out)
      onSaved?.()
      onClose()
    } catch (e) {
      const d = e?.response?.data?.detail
      if (d && typeof d === 'object' && !Array.isArray(d)) setFieldErr(d)
      setErr(e.response ? errText(e, 'Save failed') : e.message)
    } finally { setBusy(false) }
  }

  const field = ([k, label, editor]) => {
    const s = spec[k]
    if (!s) return null
    const kind = s.kind || 'str'
    const ed = editor || kind
    let control
    if (ed === 'bool') control = <Switch on={!!draft[k]} onChange={set(k)} ariaLabel={label} />
    else if (ed.startsWith('enum:')) {
      control = <Segmented size="sm" grow={false} value={draft[k]} onChange={set(k)} ariaLabel={label}
        options={ed.slice(5).split('|').map((o) => ({ value: o, label: o }))} />
    } else if (ed === 'resumes') {
      const picked = new Set(draft[k] || [])
      control = (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {!resumes.length && <Helper size="sm">No base résumés found.</Helper>}
          {resumes.map((r) => (
            <Check key={r.id} checked={picked.has(String(r.id))} label={r.name}
              onChange={(on) => {
                const n = new Set(picked); on ? n.add(String(r.id)) : n.delete(String(r.id)); set(k)([...n])
              }} />
          ))}
          <Helper size="sm">{picked.size ? `${picked.size} selected` : 'None ticked = every base résumé'}</Helper>
        </div>
      )
    } else if (ed === 'chain') {
      control = <LlmChain value={draft[k]} onChange={set(k)} label={label} catalog={catalog} efforts={efforts} usage={usage.usage} cooling={usage.cooling} />
    } else if (ed === 'agentModel') {
      const opts = [...AGENT_ALIASES, ...catalog.filter((m) => m.provider === 'claude_code').map((m) => [m.model, m.label || m.model])]
      if (draft[k] && !opts.some(([v]) => v === draft[k])) opts.push([draft[k], draft[k]])
      control = <Select value={draft[k] ?? ''} options={opts} onPick={set(k)} width="100%" mono ariaLabel={label} />
    } else if (ed === 'list') control = <Textarea mono rows={3} value={draft[k] ?? ''} onChange={set(k)} ariaLabel={label} placeholder="one per line" />
    else if (ed === 'long') control = <Textarea rows={6} value={draft[k] ?? ''} onChange={set(k)} ariaLabel={label} />
    else if (kind === 'json') control = <Textarea mono rows={3} value={draft[k] ?? ''} onChange={set(k)} ariaLabel={label} />
    else control = <Input mono={kind !== 'str'} type={kind === 'int' ? 'number' : 'text'} value={draft[k] ?? ''} onChange={set(k)} ariaLabel={label} invalid={!!fieldErr[k]} />
    return (
      <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 14 }}>
        <Label>{label}{dirty.includes(k) ? ' •' : ''}</Label>
        {control}
        {fieldErr[k] ? <Helper size="sm" style={{ color: 'var(--bad)' }}>{fieldErr[k]}</Helper>
          : s.description && <Helper size="sm">{s.description}</Helper>}
      </div>
    )
  }

  return (
    <Drawer width={560} onClose={onClose} title="AutoApply settings">
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
        <div style={{ padding: '18px 22px', overflowY: 'auto', flex: 1 }}>
          <Heading strong style={{ marginBottom: 16 }}>AutoApply settings</Heading>
          {!spec ? <Helper>{err || 'Loading…'}</Helper> : SECTIONS.map(([title, fields]) => (
            <section key={title} style={{ marginBottom: 10 }}>
              <Label size="sm" style={{ textTransform: 'uppercase', letterSpacing: '.06em', color: 'var(--muted)', margin: '6px 0 12px', display: 'block', borderBottom: '1px solid var(--line)', paddingBottom: 4 }}>{title}</Label>
              {fields.map(field)}
            </section>
          ))}
        </div>
        {spec && (
          <div style={{ padding: '12px 22px', borderTop: '1px solid var(--line)', display: 'flex', gap: 8, alignItems: 'center' }}>
            <Button onClick={save} busy={busy} disabled={!dirty.length}>Save{dirty.length ? ` ${dirty.length}` : ''}</Button>
            <Button variant="secondary" onClick={onClose}>Cancel</Button>
            {err && <Helper size="sm" style={{ color: 'var(--bad)', flex: 1 }}>{err}</Helper>}
          </div>
        )}
      </div>
    </Drawer>
  )
}
