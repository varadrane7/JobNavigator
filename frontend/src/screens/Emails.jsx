import React, { useState, useEffect, useCallback } from 'react'
import api from '../api'
import { useToasts, ToastStack } from '../Toast'
import { Button, Card, HeaderRow, Helper, Link, Mono, PageTitle, Select, Tag, Notice, Spinner } from '../ui'
import { agoShort } from '../time'
import '../theme.css'

// Emails: fresh LinkedIn "we're hiring" posts, to answer with a short email to the poster.
// JobNavigator does not find these posts or summarise them: an outside agent sends each one in
// (POST /api/hiring-posts, or the connector's add_hiring_post; docs/HIRING_POSTS.md) and this
// screen shows the post's text and the agent's summary. The target is a post under a day old
// with under 100 likes; posts outside that are kept and flagged, never hidden.

const TABS = [['new', 'To email'], ['emailed', 'Emailed'], ['skipped', 'Skipped'], ['', 'All']]
const STATUS = [['new', 'New'], ['emailed', 'Emailed'], ['skipped', 'Skipped']]
const CLIP = 360

const copy = async (text, push, what) => {
  try { await navigator.clipboard.writeText(text); push({ kind: 'success', msg: `${what} copied` }) }
  catch { push({ kind: 'error', msg: 'Clipboard unavailable' }) }
}

export function ageTag(p, maxHours = 24) {
  const age = p.checks?.age_hours
  if (age == null) return ['Age unknown', 'neutral', 'The agent did not send when the post went up']
  const label = `${agoShort(p.posted_at)} old`
  return age < maxHours ? [label, 'good', 'Under a day old'] : [label, 'warn', `Older than ${maxHours} hours`]
}

export function likesTag(p, maxLikes = 100) {
  if (p.likes == null) return null
  const label = `${p.likes} like${p.likes === 1 ? '' : 's'}`
  return p.likes < maxLikes ? [label, 'good', `Under ${maxLikes} likes`] : [label, 'warn', `${maxLikes} likes or more: crowded already`]
}

export function PostCard({ p, limits, onStatus, onDelete, push }) {
  const [open, setOpen] = useState(false)
  const long = (p.text || '').length > CLIP
  const shown = open || !long ? p.text : `${p.text.slice(0, CLIP).trimEnd()}…`
  const [ageLabel, ageTone, ageWhy] = ageTag(p, limits?.max_age_hours)
  const likes = likesTag(p, limits?.max_likes)
  const done = p.status !== 'new'
  return (
    <Card style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '12px 14px', opacity: done ? 0.65 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, flexWrap: 'wrap' }}>
        <div style={{ minWidth: 0, flex: '1 1 240px', display: 'flex', flexDirection: 'column' }}>
          {p.author_url
            ? <Link href={p.author_url} target="_blank" rel="noreferrer">{p.author_name || 'Poster'}</Link>
            : <span style={{ fontWeight: 600 }}>{p.author_name || 'Unknown poster'}</span>}
          <Helper size="xs" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={p.author_title || ''}>
            {[p.author_title, p.company, p.location].filter(Boolean).join(' · ')}
          </Helper>
        </div>
        <Tag tone={ageTone} title={ageWhy}>{ageLabel}</Tag>
        {likes && <Tag tone={likes[1]} title={likes[2]}>{likes[0]}</Tag>}
        {p.comments != null && <Tag tone="neutral">{p.comments} comment{p.comments === 1 ? '' : 's'}</Tag>}
        {p.status === 'new' && <Button size="xs" variant="primary" onClick={() => onStatus(p, 'emailed')}>Emailed</Button>}
        {p.status === 'new' && <Button size="xs" variant="ghost" onClick={() => onStatus(p, 'skipped')}>Skip</Button>}
        <Select value={p.status} options={STATUS} onPick={(v) => onStatus(p, v)} width="110px" ariaLabel="Post status" />
      </div>
      {p.roles?.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {p.roles.map((r) => <Tag key={r} tone="accent">{r}</Tag>)}
        </div>
      )}
      {p.summary
        ? <div style={{ fontSize: 'var(--t-13)', lineHeight: 1.45 }}><b>Summary:</b> {p.summary}</div>
        : <Helper size="xs">No summary sent.</Helper>}
      <div style={{ whiteSpace: 'pre-wrap', fontSize: 'var(--t-12)', lineHeight: 1.5, color: 'var(--muted)',
        borderLeft: '2px solid var(--card-border)', paddingLeft: 10 }}>{shown}</div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {long && <Button size="xs" variant="ghost" onClick={() => setOpen((v) => !v)}>{open ? 'Less ▴' : 'Full post ▾'}</Button>}
        <Button size="xs" variant="ghost" href={p.post_url} target="_blank" rel="noreferrer">Open post</Button>
        {p.email
          ? <><Mono size="xs">{p.email}</Mono><Button size="xs" variant="ghost" onClick={() => copy(p.email, push, 'Email')}>Copy email</Button></>
          : <Helper size="xs">No email found</Helper>}
        <Button size="xs" variant="ghost" onClick={() => copy(p.text, push, 'Post text')}>Copy text</Button>
        <Button size="xs" variant="ghost" onClick={() => onDelete(p)} title="Remove this post from the list">Delete</Button>
        <Helper size="xs" style={{ marginLeft: 'auto' }}>
          added {agoShort(p.added_at)} ago{p.agent ? ` · by ${p.agent}` : ''}
        </Helper>
      </div>
    </Card>
  )
}

export default function Emails() {
  const { toasts, push, dismiss } = useToasts()
  const [tab, setTab] = useState('new')
  const [fresh, setFresh] = useState(false)
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)

  const load = useCallback(() => {
    setErr(null)
    const params = { limit: 200, ...(tab ? { status: tab } : {}), ...(fresh ? { fresh: true } : {}) }
    api.get('/hiring-posts', { params })
      .then(({ data: d }) => setData(d))
      .catch((e) => setErr(e?.response?.data?.detail || 'Could not load posts'))
  }, [tab, fresh])
  useEffect(() => { load() }, [load])

  const setStatus = async (p, status) => {
    try {
      const { data: row } = await api.patch(`/hiring-posts/${p.id}`, { status })
      setData((d) => ({
        ...d,
        counts: { ...d.counts, [p.status]: (d.counts[p.status] || 1) - 1, [status]: (d.counts[status] || 0) + 1 },
        posts: tab && tab !== status ? d.posts.filter((x) => x.id !== p.id) : d.posts.map((x) => (x.id === p.id ? row : x)),
      }))
    } catch { push({ kind: 'error', msg: 'Could not update' }) }
  }
  const remove = async (p) => {
    try {
      await api.delete(`/hiring-posts/${p.id}`)
      setData((d) => ({ ...d, counts: { ...d.counts, [p.status]: Math.max(0, (d.counts[p.status] || 1) - 1) },
        posts: d.posts.filter((x) => x.id !== p.id) }))
    } catch { push({ kind: 'error', msg: 'Could not delete' }) }
  }

  const counts = data?.counts || {}
  const options = TABS.map(([k, l]) => [k, k ? `${l} · ${counts[k] ?? 0}` : l])
  return (
    <div style={{ position: 'relative', flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <HeaderRow as="header" variant="screen" align="flex-end" style={{ gap: 18, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
          <PageTitle>Emails</PageTitle>
          <Helper>Fresh “we're hiring” posts. Email the poster directly, under five lines, before the post fills up. Do not DM or fill the form.</Helper>
        </div>
      </HeaderRow>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '10px 30px 0', flexWrap: 'wrap' }}>
        <Select value={tab} options={options} onPick={setTab} width="170px" ariaLabel="Which posts" />
        <Select value={fresh ? 'fresh' : 'any'} options={[['any', 'Any age'], ['fresh', 'Under a day old']]}
          onPick={(v) => setFresh(v === 'fresh')} width="160px" ariaLabel="Post age" />
        {data && <Helper size="xs">{data.total} post{data.total === 1 ? '' : 's'}</Helper>}
      </div>
      <div className="v2-scroll" style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: '12px 30px 40px', display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 1000 }}>
        {err && <Notice tone="bad">{err}</Notice>}
        {!data && !err && <Spinner />}
        {data && data.posts.length === 0 && (
          <Helper>No posts here yet. An agent adds them through the API (POST /api/hiring-posts) or the connector tool add_hiring_post.</Helper>
        )}
        {data?.posts.map((p) => <PostCard key={p.id} p={p} limits={data.limits} onStatus={setStatus} onDelete={remove} push={push} />)}
      </div>
      <ToastStack toasts={toasts} onClose={dismiss} />
    </div>
  )
}
