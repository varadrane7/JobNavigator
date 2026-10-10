import React, { useEffect, useState } from 'react'
import { Button, Drawer, Heading, Helper, Input, Label, Segmented, Switch, Textarea } from '../ui'
import { getSettings, putSettings, errText } from './api'

// Which settings the drawer edits, in display order. Anything not listed keeps
// its stored value (the API only writes keys it is sent).
const FIELDS = [
  ['autoapply_enabled', 'Enabled'],
  ['autoapply_mode', 'Submit gate'],
  ['autoapply_dry_run', 'Dry run (never click submit)'],
  ['autoapply_daily_cap', 'Daily cap'],
  ['autoapply_min_score', 'Min score'],
  ['autoapply_max_age_days', 'Max job age (days)'],
  ['autoapply_score_budget', 'Scoring budget per build'],
  ['autoapply_company_daily_cap', 'Per-company daily cap'],
  ['autoapply_queue_cron', 'Queue build cron'],
  ['autoapply_jitter_min_s', 'Jitter min (s)'],
  ['autoapply_jitter_max_s', 'Jitter max (s)'],
  ['autoapply_quiet_hours', 'Quiet hours (start-end)'],
  ['autoapply_cover_letter', 'Cover letter'],
  ['autoapply_allow_new_skills', 'Allow new skills when tailoring'],
  ['autoapply_site_caps', 'Per-site caps (JSON)'],
  ['autoapply_enabled_submitters', 'Enabled submitters (JSON)'],
  ['autoapply_resume_ids', 'Résumé ids (JSON, empty = all base)'],
  ['autoapply_company_blocklist', 'Company blocklist (JSON)'],
  ['autoapply_title_blocklist', 'Title blocklist (JSON)'],
  ['autoapply_cdp_url', 'Chrome CDP URL'],
]

export default function AutoApplySettings({ onClose, onSaved }) {
  const [spec, setSpec] = useState(null)
  const [draft, setDraft] = useState({})
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    getSettings().then((d) => {
      setSpec(d.spec)
      const v = {}
      for (const [k] of FIELDS) {
        const kind = d.spec[k]?.kind
        v[k] = kind === 'json' ? JSON.stringify(d.values[k], null, 0) : d.values[k]
      }
      setDraft(v)
    }).catch((e) => setErr(errText(e, 'Could not load settings')))
  }, [])

  const set = (k) => (val) => setDraft((p) => ({ ...p, [k]: val }))

  const save = async () => {
    setBusy(true); setErr('')
    try {
      const out = {}
      for (const [k] of FIELDS) {
        if (spec[k]?.kind === 'json') {
          try { out[k] = JSON.parse(draft[k] || 'null') } catch { throw new Error(`${k}: not valid JSON`) }
        } else out[k] = draft[k]
      }
      await putSettings(out)
      onSaved?.()
      onClose()
    } catch (e) {
      setErr(e.message && !e.response ? e.message : errText(e, 'Save failed'))
    } finally { setBusy(false) }
  }

  const field = (k, label) => {
    const s = spec[k] || {}
    const kind = s.kind || 'str'
    let control
    if (kind === 'bool') control = <Switch on={!!draft[k]} onChange={set(k)} ariaLabel={label} />
    else if (kind.startsWith('enum:')) {
      control = <Segmented size="sm" grow={false} value={draft[k]} onChange={set(k)} ariaLabel={label}
        options={kind.slice(5).split('|').map((o) => ({ value: o, label: o }))} />
    } else if (kind === 'json') control = <Textarea mono rows={2} value={draft[k] ?? ''} onChange={(e) => set(k)(e.target.value)} ariaLabel={label} />
    else control = <Input mono={kind !== 'str'} value={draft[k] ?? ''} onChange={(e) => set(k)(e.target.value)} ariaLabel={label} />
    return (
      <div key={k} style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 14 }}>
        <Label>{label}</Label>
        {control}
        {s.description && <Helper size="sm">{s.description}</Helper>}
      </div>
    )
  }

  return (
    <Drawer width={520} onClose={onClose} title="AutoApply settings">
      <div style={{ padding: '18px 22px', overflowY: 'auto' }}>
        <Heading strong style={{ marginBottom: 16 }}>AutoApply settings</Heading>
        {!spec ? <Helper>{err || 'Loading…'}</Helper> : FIELDS.map(([k, l]) => field(k, l))}
        {spec && err && <Helper style={{ color: 'var(--bad)' }}>{err}</Helper>}
        {spec && (
          <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
            <Button onClick={save} busy={busy}>Save</Button>
            <Button variant="secondary" onClick={onClose}>Cancel</Button>
          </div>
        )}
      </div>
    </Drawer>
  )
}
