import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useToasts, ToastStack } from '../Toast'
import { Button, Check, Helper, Input, Link, Mono, Notice, PageTitle, Segmented, Tag, scoreTone } from '../ui'
import '../theme.css'
import { getItems, getSummary, itemAction, rebuildQueue, cdpStatus, resumeSite, errText } from './api'
import AutoApplySettings from './AutoApplySettings'
import ItemDrawer from './ItemDrawer'

// Tabs mirror Tsenta's tracker: what needs a decision, what is moving, what landed.
const TABS = [
  { value: 'queue', label: 'Queue', statuses: ['queued'] },
  { value: 'flight', label: 'In flight', statuses: ['approved', 'preparing', 'ready', 'submitting'] },
  { value: 'needs', label: 'Needs you', statuses: ['needs_human', 'dry_run_ok'] },
  { value: 'submitted', label: 'Submitted', statuses: ['submitted'] },
  { value: 'failed', label: 'Failed', statuses: ['failed'] },
  { value: 'skipped', label: 'Skipped', statuses: ['rejected', 'skipped'] },
]
// Actions offered per tab; the API refuses any transition a row's status does not allow.
const TAB_ACTIONS = {
  queue: ['approve', 'reject', 'skip'],
  flight: ['reject', 'skip'],
  needs: ['submit', 'reject', 'skip'],
  submitted: [],
  failed: ['submit', 'approve', 'skip'],
  skipped: ['requeue'],
}
// approve on a failed item rebuilds its documents; submit retries the form with the ones it has.
const ACTION_LABEL = { approve: 'Approve', reject: 'Reject', skip: 'Skip', requeue: 'Requeue', submit: 'Retry submit' }
const STATUS_TONE = {
  queued: 'neutral', approved: 'accent', preparing: 'accent', ready: 'accent', submitting: 'accent',
  submitted: 'good', dry_run_ok: 'warn', needs_human: 'warn', failed: 'bad', rejected: 'neutral', skipped: 'neutral',
}
const today = () => new Date().toISOString().slice(0, 10)

export default function AutoApplyQueue() {
  const { toasts, push, dismiss } = useToasts()
  const [runDate, setRunDate] = useState(today())
  const [allDays, setAllDays] = useState(false)
  const [tab, setTab] = useState('queue')
  const [items, setItems] = useState([])
  const [summary, setSummary] = useState(null)
  const [sel, setSel] = useState(() => new Set())
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [showSettings, setShowSettings] = useState(false)
  const [openId, setOpenId] = useState(null)

  const statuses = useMemo(() => TABS.find((t) => t.value === tab).statuses, [tab])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [rows, sum] = await Promise.all([
        getItems({ run_date: allDays ? undefined : runDate, status: statuses.join(',') }),
        getSummary(runDate),
      ])
      setItems(rows)
      setSummary(sum)
      setSel(new Set())
    } catch (e) {
      push({ kind: 'error', msg: errText(e, 'Could not load the queue') })
    } finally { setLoading(false) }
  }, [runDate, allDays, statuses, push])

  useEffect(() => { load() }, [load])

  const act = async (action) => {
    if (!sel.size) return
    setBusy(action)
    try {
      const r = await itemAction(action, [...sel])
      push({ kind: 'success', msg: `${ACTION_LABEL[action]}: ${r.changed.length} changed${r.unchanged.length ? `, ${r.unchanged.length} not allowed` : ''}` })
      await load()
    } catch (e) {
      push({ kind: 'error', msg: errText(e, `${ACTION_LABEL[action]} failed`) })
    } finally { setBusy('') }
  }

  const rebuild = async () => {
    setBusy('rebuild')
    try {
      await rebuildQueue()
      push({ kind: 'progress', msg: 'Building today’s queue — scoring may take a few minutes' })
    } catch (e) {
      push({ kind: 'error', msg: errText(e, 'Queue build failed to start') })
    } finally { setBusy('') }
  }

  const checkChrome = async () => {
    setBusy('cdp')
    try {
      const st = await cdpStatus()
      if (!st.reachable) push({ kind: 'error', msg: `Chrome not reachable at ${st.cdp_url}: ${st.error || 'no answer'}` })
      else push({ kind: 'success', msg: `Chrome ${st.version} reachable · LinkedIn ${st.linkedin_logged_in ? 'logged in' : 'NOT logged in'} · ZipRecruiter cookies: ${st.ziprecruiter_cookies}` })
    } catch (e) {
      push({ kind: 'error', msg: errText(e, 'Status check failed') })
    } finally { setBusy('') }
  }

  const unpause = async (site) => {
    try { await resumeSite(site); await load() } catch (e) { push({ kind: 'error', msg: errText(e, 'Could not resume') }) }
  }

  const toggle = (id) => setSel((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n })
  const allOn = items.length > 0 && sel.size === items.length
  const counts = summary?.by_status || {}
  const tabCount = (t) => t.statuses.reduce((a, s) => a + (counts[s] || 0), 0)

  return (
    <div style={{ position: 'relative', height: '100%', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{ padding: '22px 30px 12px', display: 'flex', alignItems: 'flex-end', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <PageTitle>AutoApply</PageTitle>
          {summary && (
            <Helper style={{ marginTop: 8 }}>
              {summary.total}/{summary.daily_cap} queued for {summary.run_date}
              {' · '}gate: {summary.mode}{summary.dry_run ? ' · dry run' : ''}
              {!summary.enabled && ' · scheduler off (enable in settings)'}
            </Helper>
          )}
        </div>
        <Input type="date" value={runDate} onChange={(e) => setRunDate(e.target.value || today())} width={150} ariaLabel="Run date" />
        <Check checked={allDays} onChange={setAllDays} label="All days" />
        <Button variant="secondary" onClick={rebuild} busy={busy === 'rebuild'}>Build today’s queue</Button>
        <Button variant="ghost" onClick={checkChrome} busy={busy === 'cdp'}>Check Chrome</Button>
        <Button variant="ghost" onClick={() => setShowSettings(true)}>Settings</Button>
      </div>
      {Object.entries(summary?.paused_sites || {}).map(([site, until]) => (
        <Notice key={site} tone="warn" style={{ margin: '0 30px 10px' }}
          action={<Link onClick={() => unpause(site)}>Resume now</Link>}>
          {site} paused until {new Date(until).toLocaleString()} after a checkpoint. Clear the check in your Chrome first.
        </Notice>
      ))}

      <div style={{ padding: '0 30px 12px', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <Segmented grow={false} value={tab} onChange={setTab} ariaLabel="Queue view"
          options={TABS.map((t) => ({ value: t.value, label: `${t.label}${tabCount(t) ? ` ${tabCount(t)}` : ''}` }))} />
        <div style={{ flex: 1 }} />
        {TAB_ACTIONS[tab].map((a) => (
          <Button key={a} size="sm" variant={a === 'approve' || a === 'requeue' || a === 'submit' ? 'primary' : 'secondary'}
            disabled={!sel.size} busy={busy === a} onClick={() => act(a)}>
            {ACTION_LABEL[a]}{sel.size ? ` ${sel.size}` : ''}
          </Button>
        ))}
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '0 30px 30px' }}>
        {loading ? <Helper>Loading…</Helper> : !items.length ? (
          <Helper>Nothing here{tab === 'queue' ? ' — build today’s queue, or lower the min score in settings.' : '.'}</Helper>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--t-12)' }}>
            <thead>
              <tr style={{ textAlign: 'left', color: 'var(--muted)' }}>
                <th style={{ width: 28, padding: '6px 4px' }}>
                  <Check checked={allOn} indeterminate={sel.size > 0 && !allOn}
                    onChange={() => setSel(allOn ? new Set() : new Set(items.map((i) => i.id)))} ariaLabel="Select all" />
                </th>
                <th style={{ padding: '6px 4px', width: 36 }}>#</th>
                <th style={{ padding: '6px 4px', width: 52 }}>Fit</th>
                <th style={{ padding: '6px 4px' }}>Job</th>
                <th style={{ padding: '6px 4px' }}>Résumé</th>
                <th style={{ padding: '6px 4px' }}>Via</th>
                <th style={{ padding: '6px 4px' }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.id} style={{ borderTop: '1px solid var(--line-soft, var(--line))' }}>
                  <td style={{ padding: '7px 4px' }}><Check checked={sel.has(it.id)} onChange={() => toggle(it.id)} ariaLabel="Select" /></td>
                  <td style={{ padding: '7px 4px' }}><Mono size="sm">{it.rank ?? ''}</Mono></td>
                  <td style={{ padding: '7px 4px' }}><Tag tone={scoreTone(it.score)}>{it.score != null ? Math.round(it.score) : '–'}</Tag></td>
                  <td style={{ padding: '7px 4px', minWidth: 240 }}>
                    <div style={{ fontWeight: 600, cursor: 'pointer' }} onClick={() => setOpenId(it.id)} title="Open details">{it.company}</div>
                    <Link href={it.url} target="_blank" rel="noopener noreferrer">{it.title}</Link>
                    {it.location && <Helper size="sm">{it.location}</Helper>}
                    {(it.flags || []).length > 0 && <Helper size="sm" style={{ color: 'var(--warn)' }}>{it.flags.length} flag(s) — open for details</Helper>}
                    {it.error && <Helper size="sm" style={{ color: 'var(--bad)' }}>{it.error}</Helper>}
                  </td>
                  <td style={{ padding: '7px 4px' }} title={Object.entries(it.cv_scores || {}).map(([k, v]) => `${k}: ${v}`).join('\n')}>
                    {it.base_resume_name}
                  </td>
                  <td style={{ padding: '7px 4px' }}><Tag tone="neutral" title={it.site_host}>{it.submitter}</Tag></td>
                  <td style={{ padding: '7px 4px' }}><Tag tone={STATUS_TONE[it.status] || 'neutral'}>{it.status.replace('_', ' ')}</Tag></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {openId && <ItemDrawer itemId={openId} onClose={() => setOpenId(null)} onChanged={load} push={push} />}
      {showSettings && <AutoApplySettings onClose={() => setShowSettings(false)} onSaved={load} />}
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
