import React, { useEffect, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { Helper, Heading, Mono, Notice, PageTitle, Segmented } from '../ui'
import '../theme.css'
import { getStats, errText } from './api'

const pct = (v) => (v == null ? '–' : `${v}%`)
const dur = (s) => (s == null ? '–' : s >= 90 ? `${(s / 60).toFixed(1)} min` : `${Math.round(s)} s`)

function Figure({ label, value, hint }) {
  return (
    <div style={{ minWidth: 150 }}>
      <Helper size="sm">{label}</Helper>
      <div style={{ fontSize: 'var(--t-24, 24px)', fontWeight: 600 }}>{value}</div>
      {hint && <Helper size="sm">{hint}</Helper>}
    </div>
  )
}

function DailyBars({ rows }) {
  const max = Math.max(1, ...rows.map((r) => r.submitted))
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 110 }}>
      {rows.map((r) => (
        <div key={r.day} title={`${r.day}: ${r.submitted}`} style={{ flex: 1, textAlign: 'center' }}>
          <div style={{ height: Math.max(2, (r.submitted / max) * 90), background: 'var(--accent, #4b7bec)', borderRadius: 3 }} />
          <Mono size="sm">{r.submitted}</Mono>
        </div>
      ))}
    </div>
  )
}

const th = { textAlign: 'left', padding: '4px 14px 4px 0', color: 'var(--muted)', fontWeight: 500 }
const td = { padding: '4px 14px 4px 0' }

export default function AutoApplyStats() {
  const [days, setDays] = useState(14)
  const [d, setD] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    setErr('')
    getStats(days).then(setD).catch((e) => setErr(errText(e, 'Could not load stats')))
  }, [days])

  const t = d?.totals
  return (
    <div style={{ padding: '24px 32px', maxWidth: 1100 }}>
      <PageTitle>AutoApply stats</PageTitle>
      <div style={{ display: 'flex', gap: 16, alignItems: 'center', margin: '8px 0 20px' }}>
        <Segmented grow={false} value={days} onChange={setDays} ariaLabel="Window"
          options={[{ value: 7, label: '7 days' }, { value: 14, label: '14 days' }, { value: 30, label: '30 days' }]} />
        <RouterLink to="/autoapply">← Queue</RouterLink>
      </div>
      {err && <Notice tone="bad">{err}</Notice>}
      {!d ? <Helper>Loading…</Helper> : (
        <>
          <div style={{ display: 'flex', gap: 32, flexWrap: 'wrap', marginBottom: 28 }}>
            <Figure label="Submitted" value={t.submitted} hint={`${t.queued} queued`} />
            <Figure label="Needed you" value={t.needs_human} hint="parked for a person" />
            <Figure label="Failed" value={t.failed} />
            <Figure label="Got a response" value={pct(t.response_rate)} hint={`${t.responses} of ${t.submitted}`} />
            <Figure label="Time per app" value={dur(t.avg_seconds_per_app)} />
            <Figure label="LLM calls per app" value={t.llm_calls_per_app ?? '–'} />
          </div>

          <Heading>Submitted per day</Heading>
          <DailyBars rows={d.daily} />

          <Heading style={{ marginTop: 28 }}>By submitter</Heading>
          <table style={{ borderCollapse: 'collapse', fontSize: 'var(--t-13, 13px)' }}>
            <thead><tr>{['Submitter', 'Total', 'Submitted', 'Failed', 'Needs you', 'Success', 'Needs-you rate'].map((h) => <th key={h} style={th}>{h}</th>)}</tr></thead>
            <tbody>
              {d.by_submitter.map((s) => (
                <tr key={s.submitter}>
                  <td style={td}>{s.submitter}</td><td style={td}>{s.total}</td><td style={td}>{s.submitted}</td>
                  <td style={td}>{s.failed}</td><td style={td}>{s.needs_human}</td>
                  <td style={td}>{pct(s.success_rate)}</td><td style={td}>{pct(s.needs_human_rate)}</td>
                </tr>
              ))}
              {!d.by_submitter.length && <tr><td style={td} colSpan={7}><Helper>No applications in this window.</Helper></td></tr>}
            </tbody>
          </table>

          <Heading style={{ marginTop: 28 }}>LLM use</Heading>
          <table style={{ borderCollapse: 'collapse', fontSize: 'var(--t-13, 13px)' }}>
            <thead><tr>{['Provider', 'Calls', 'Failures', 'Quota hits', 'Tokens in', 'Tokens out'].map((h) => <th key={h} style={th}>{h}</th>)}</tr></thead>
            <tbody>
              {d.llm.map((p) => (
                <tr key={p.provider}>
                  <td style={td}>{p.provider}</td><td style={td}>{p.calls}</td><td style={td}>{p.failures}</td>
                  <td style={td}>{p.quota_hits}</td><td style={td}>{p.input_tokens}</td><td style={td}>{p.output_tokens}</td>
                </tr>
              ))}
              {!d.llm.length && <tr><td style={td} colSpan={6}><Helper>No LLM calls yet.</Helper></td></tr>}
            </tbody>
          </table>
          <Helper style={{ marginTop: 10 }}>
            {d.cost.note} Tsenta charges about ${d.cost.tsenta_per_app_usd[0]}–${d.cost.tsenta_per_app_usd[1]} per application.
          </Helper>
        </>
      )}
    </div>
  )
}
