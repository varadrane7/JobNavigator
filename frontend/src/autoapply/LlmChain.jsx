import React from 'react'
import { Helper, Label, Select, Tag } from '../ui'

// Same providers and labels as the main Settings › AI Models rows.
export const PROVIDERS = [
  ['claude_api', 'Claude API (Anthropic)'],
  ['claude_code', 'Claude Code (Subscription)'],
  ['codex_cli', 'Codex CLI (ChatGPT Subscription)'],
  ['antigravity_cli', 'Antigravity CLI (Google Subscription)'],
  ['openai', 'OpenAI'],
  ['openai_local', 'OpenAI (Local)'],
  ['ollama', 'Ollama (Local)'],
  ['lmstudio', 'LM Studio (Local)'],
  ['openrouter', 'OpenRouter'],
]
const LOCAL = ['openai_local', 'ollama', 'lmstudio']
const SUBSCRIPTION = ['claude_code', 'codex_cli', 'antigravity_cli']
export const SLOTS = 3

// Rough limit weight of a model, read off its name: what a pick costs against a plan's limit.
const weight = (provider, model) => {
  if (LOCAL.includes(provider)) return ['local · free', 'good']
  if (!model) return SUBSCRIPTION.includes(provider) ? ['provider default · often heavy', 'bad'] : ['provider default', 'neutral']
  const m = model.toLowerCase()
  if (/opus|-pro|thinking|astra|\bmax\b|70b|120b|o3\b/.test(m)) return ['heavy', 'bad']
  if (/haiku|flash-low|flash-lite|mini|nano|luna|[0-9]b\b|:\d+b/.test(m)) return ['light', 'good']
  return ['standard', 'warn']
}

// Stored chain -> exactly SLOTS editable slots; empty slots carry provider ''.
export const toSlots = (chain) => {
  const s = (Array.isArray(chain) ? chain : []).slice(0, SLOTS)
    .map((e) => ({ provider: e?.provider || '', model: e?.model || '', effort: e?.effort || '' }))
  while (s.length < SLOTS) s.push({ provider: '', model: '', effort: '' })
  return s
}
export const fromSlots = (slots) => (slots || []).filter((e) => e.provider)
  .map(({ provider, model, effort }) => (effort ? { provider, model, effort } : { provider, model }))

export function LlmChain({ value, onChange, label, catalog, efforts, usage, cooling }) {
  const slots = value || toSlots([])
  const put = (i, patch) => onChange(slots.map((s, j) => (j === i ? { ...s, ...patch } : s)))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {slots.map((s, i) => {
        const models = catalog.filter((m) => m.provider === s.provider).map((m) => [m.model, m.label || m.model])
        if (s.model && !models.some(([v]) => v === s.model)) models.unshift([s.model, s.model])
        const modelOpts = [['', LOCAL.includes(s.provider) ? 'whatever is loaded' : 'provider default'], ...models]
        const eff = efforts[s.provider] || []
        const [w, tone] = weight(s.provider, s.model)
        const u = usage.filter((r) => r.provider === s.provider && (r.model || '') === s.model)
          .reduce((a, r) => ({ calls: a.calls + (r.calls || 0), quota: a.quota + (r.quota_hits || 0), fail: a.fail + (r.failures || 0) }), { calls: 0, quota: 0, fail: 0 })
        const cool = s.provider && cooling[s.provider]
        return (
          <div key={i} style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Label size="sm" style={{ width: 72, flex: '0 0 auto' }}>Priority {i + 1}</Label>
              <Select value={s.provider} options={[['', '— off —'], ...PROVIDERS]} width="200px" ariaLabel={`${label} — priority ${i + 1} provider`}
                onPick={(v) => put(i, { provider: v, model: '', effort: (efforts[v] || []).includes(s.effort) ? s.effort : '' })} />
              {s.provider && <Select value={s.model} options={modelOpts} onPick={(v) => put(i, { model: v })} width="200px" mono
                ariaLabel={`${label} — priority ${i + 1} model`} />}
            </div>
            {s.provider && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingLeft: 80, flexWrap: 'wrap' }}>
                {eff.length > 0 && <Select value={s.effort} options={[['', 'default effort'], ...eff.map((v) => [v, `${v} effort`])]}
                  onPick={(v) => put(i, { effort: v })} width="140px" ariaLabel={`${label} — priority ${i + 1} effort`} />}
                <Tag tone={tone}>{w}</Tag>
                <Helper size="sm">7 days: {u.calls} calls{u.quota ? ` · ${u.quota} limit hits` : ''}{u.fail - u.quota > 0 ? ` · ${u.fail - u.quota} errors` : ''}</Helper>
                {cool && <Tag tone="warn" title="Skipped until then after a limit hit or error">cooling until {new Date(cool).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</Tag>}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
