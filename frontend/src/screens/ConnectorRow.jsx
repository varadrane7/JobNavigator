import React, { useEffect, useState } from 'react'
import api from '../api'
import { Helper, Mono, Pill } from '../ui'

// Settings › Advanced › Connector: the MCP address of this install (backend/connector).
// claude.ai signs in with OAuth from that address alone; a token is for clients that cannot
// (Claude Code, other agents). The server shows a token once and keeps no copy.
const CODE = { fontSize: 11.5, userSelect: 'all', overflowWrap: 'anywhere' }

export default function ConnectorRow({ flash }) {
  const [info, setInfo] = useState(null)
  const [made, setMade] = useState(null)
  const [busy, setBusy] = useState('')

  useEffect(() => {
    let live = true
    api.get('/connector').then((r) => { if (live) setInfo(r?.data || null) }).catch(() => {})
    return () => { live = false }
  }, [])

  const copy = async (text, what) => {
    try { await navigator.clipboard.writeText(text); flash(`${what} copied`) }
    catch { flash('Copy failed: select the text and copy it', true) }
  }
  const make = async (scope) => {
    setBusy(scope)
    try { setMade((await api.post('/connector/token', { scope, label: 'settings' })).data) }
    catch { flash('Could not make a token', true) }
    finally { setBusy('') }
  }
  const revoke = async () => {
    setBusy('revoke')
    try { await api.post('/connector/revoke'); setMade(null); flash('Every connected app was disconnected') }
    catch { flash('Could not disconnect', true) }
    finally { setBusy('') }
  }

  if (!info) return <Helper>Loading…</Helper>
  return (
    <div style={{ display: 'grid', gap: 8, minWidth: 0 }}>
      <Mono style={CODE}>{info.url}</Mono>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <Pill onClick={() => copy(info.url, 'Address')} ariaLabel="Copy the connector address">Copy address</Pill>
        <Pill onClick={() => make('read')} ariaLabel="Make a read-only connector token">
          {busy === 'read' ? 'Making…' : 'Read token'}</Pill>
        <Pill onClick={() => make('write')} ariaLabel="Make a connector token that can also update leads and applications">
          {busy === 'write' ? 'Making…' : 'Read + update token'}</Pill>
        <Pill onClick={revoke} ariaLabel="Disconnect every connected app and token">
          {busy === 'revoke' ? 'Disconnecting…' : 'Disconnect all'}</Pill>
      </div>
      {made && (
        <>
          <Helper>Shown once. {made.scope === 'write' ? 'Reads, and updates leads and applications.' : 'Read-only.'} Valid for a year.</Helper>
          <Mono style={CODE}>{made.token}</Mono>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Pill onClick={() => copy(made.token, 'Token')} ariaLabel="Copy the connector token">Copy token</Pill>
            <Pill onClick={() => copy(`claude mcp add --transport http jobnavigator ${made.url} --header "Authorization: Bearer ${made.token}"`, 'Command')}
              ariaLabel="Copy the Claude Code command that adds this connector">Copy Claude Code command</Pill>
          </div>
        </>
      )}
    </div>
  )
}
