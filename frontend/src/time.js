// One clock for every v2 screen, so the list and the editor never disagree about the same timestamp.
// APP_TZ is the zone the backend runs its crons and day buckets in (backend/tz.py);
// every absolute date or time the UI shows is read in it, whatever the viewer's zone.
export const APP_TZ = import.meta.env?.VITE_APP_TZ || 'America/New_York'

// Wall-clock fields of an instant in APP_TZ: { y, mo (1-12), d, h, mi, dow (0=Sun) }.
const _partsFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: APP_TZ, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
  hour: 'numeric', minute: 'numeric', weekday: 'short',
})
const _DOW = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }
export const tzParts = (date) => {
  const o = {}
  for (const p of _partsFmt.formatToParts(date)) o[p.type] = p.value
  return { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour % 24, mi: +o.minute, dow: _DOW[o.weekday] }
}
// YYYY-MM-DD of an instant in APP_TZ (default now) — the key the backend's day buckets use.
export const tzDateKey = (date = new Date()) => {
  const p = tzParts(date)
  return `${p.y}-${String(p.mo).padStart(2, '0')}-${String(p.d).padStart(2, '0')}`
}
// The instant at which APP_TZ's wall clock reads y-mo-d h:mi.
export const zonedInstant = (y, mo, d, h = 0, mi = 0) => {
  const want = Date.UTC(y, mo - 1, d, h, mi)
  let t = want
  for (let i = 0; i < 2; i++) {
    const p = tzParts(new Date(t))
    t += want - Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi)
  }
  return new Date(t)
}
export const TZ_SHORT = (() => {
  try { return new Intl.DateTimeFormat('en-US', { timeZone: APP_TZ, timeZoneName: 'short' }).formatToParts(new Date()).find((x) => x.type === 'timeZoneName')?.value || APP_TZ }
  catch { return APP_TZ }
})()
const mins = (iso) => (iso ? Math.floor((Date.now() - new Date(iso).getTime()) / 60000) : null)
export const ago = (iso) => {
  const m = mins(iso); if (m == null) return ''
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  return h < 24 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`
}
export const agoShort = (iso) => {
  const m = mins(iso); if (m == null) return ''
  if (m < 1) return 'now'
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`
}

// whenShort's "Sat 06 Sep 05:00" shape matches Stats' toLocaleString('en-GB')
// output by hand, since ICU's own formatting (e.g. "Sept") would make the two halves of a schedule line disagree on how a day is named.

// ── cron ────────────────────────────────────────────────────────────────────
// describeCron(expr): plain-English sentence + next fire time for one Scheduler
// cron field. Pure, no dependency. Two backend-verified departures from standard
// cron: weekday numbers are APScheduler's, not vixie cron's (0=Monday — `0 8 * * 1`
// fires Tuesday in the container), and day-of-month/day-of-week are ANDed, not
// ORed (`next` follows APScheduler). `next` is computed on APP_TZ's wall clock (the
// scheduler runs in APP_TZ) and returned as a Date; callers render it via `whenShort`,
// which also reads APP_TZ. Returns `{ text, next }`, or null if unreadable;
// a five-field APScheduler-only extension (`last`, `fri#3`) this parser doesn't
// evaluate is echoed as `{ text: <raw line>, unparsed: true }` rather than called
// invalid — see CRON_DAY_EXTRA for exactly which ones, and why the rest are not.
const DOW_NAMES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']   // APScheduler order: index 0 = Monday
const DOW_FULL = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
const DOW_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const MON_NAMES = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
const MON_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

const pad2 = (n) => String(n).padStart(2, '0')
export const whenShort = (d) => {
  if (!(d instanceof Date) || Number.isNaN(d.getTime())) return ''
  const p = tzParts(d)
  return `${DOW_SHORT[(p.dow + 6) % 7]} ${pad2(p.d)} ${MON_SHORT[p.mo - 1]} ${pad2(p.h)}:${pad2(p.mi)}`
}
const ord = (n) => {
  const s = ['th', 'st', 'nd', 'rd']; const v = n % 100
  return `${n}${s[(v - 20) % 10] || s[v] || s[0]}`
}

// one token → its number, by digits or by three-letter name. Out of range is a
// hard failure (the backend refuses the same value), never a clamp.
const tokenNum = (t, min, max, names) => {
  let v = NaN
  if (/^\d+$/.test(t)) v = parseInt(t, 10)
  else if (names) { const i = names.indexOf(t.slice(0, 3)); if (i >= 0) v = i + min }
  return Number.isInteger(v) && v >= min && v <= max ? v : null
}

// `*` · `a` · `a-b` · `*/n` · `a-b/n` · `a/n` · any of those in a comma list.
// Returns the expanded set plus the two shapes the sentence builder asks about:
// `star` (the whole field is `*`) and `step` (the whole field is `*/n`).
function parseField(spec, min, max, names) {
  const raw = String(spec).trim().toLowerCase()
  if (!raw) return null
  const out = new Set()
  for (const term of raw.split(',')) {
    const slash = term.split('/')
    if (slash.length > 2) return null
    const body = slash[0]
    let step = 1
    if (slash.length === 2) {
      if (!/^\d+$/.test(slash[1])) return null
      step = parseInt(slash[1], 10)
      if (step < 1) return null
    }
    let lo; let hi
    if (body === '*') { lo = min; hi = max } else {
      const bits = body.split('-')
      if (bits.length > 2) return null
      const a = tokenNum(bits[0], min, max, names)
      if (a === null) return null
      if (bits.length === 1) { lo = a; hi = slash.length === 2 ? max : a } else {
        const b = tokenNum(bits[1], min, max, names)
        if (b === null) return null
        lo = a; hi = b
      }
    }
    if (lo > hi) return null                       // no wrap-around: APScheduler refuses it too
    for (let v = lo; v <= hi; v += step) out.add(v)
  }
  const list = [...out].sort((a, b) => a - b)
  if (!list.length) return null
  const stepAll = /^\*\/(\d+)$/.exec(raw)
  return { raw, list, set: out, star: raw === '*', step: stepAll ? parseInt(stepAll[1], 10) : 0 }
}

// keep a long list out of the sentence — past eight values the raw field says
// more than its expansion does
const fieldTxt = (F) => (F.list.length <= 8 ? F.list.join(',') : F.raw)

function cronText(M, H, D, MO, W) {
  const hm = (h, m) => `${pad2(h)}:${pad2(m)}`
  const one = (F) => F.list.length === 1
  const anyDay = D.star && MO.star && W.star

  // the seven shapes worth a plain sentence
  if (anyDay) {
    if (H.star && (M.star || M.step === 1)) return 'every minute'
    if (H.star && M.step) return `every ${M.step} minutes`
    if (H.star && one(M)) return `hourly at :${pad2(M.list[0])}`
    if (H.step && one(M)) {
      const base = H.step === 1 ? 'every hour' : `every ${H.step} hours`
      return M.list[0] === 0 ? base : `${base} at :${pad2(M.list[0])}`
    }
    if (one(M) && one(H)) return `daily at ${hm(H.list[0], M.list[0])}`
  }
  if (one(M) && one(H)) {
    const at = `at ${hm(H.list[0], M.list[0])}`
    if (D.star && MO.star && !W.star) {
      const k = W.list.join(',')
      if (k === '0,1,2,3,4') return `weekdays ${at}`         // 0 = Monday (see the note above)
      if (k === '5,6') return `weekends ${at}`
      if (one(W)) return `every ${DOW_FULL[W.list[0]]} ${at}`
      return `every ${W.list.map((d) => DOW_SHORT[d]).join(', ')} ${at}`
    }
    if (MO.star && W.star && one(D)) return `monthly on the ${ord(D.list[0])} ${at}`
  }

  // …and the generic reading for everything else
  let timePart
  const times = []
  for (const h of H.list) for (const m of M.list) times.push(hm(h, m))
  if (one(M) && one(H)) timePart = `at ${hm(H.list[0], M.list[0])}`
  else if (one(M) && H.star) timePart = `at :${pad2(M.list[0])} every hour`
  else if (one(M) && H.step) timePart = `at :${pad2(M.list[0])} every ${H.step} hours`
  else if (times.length <= 6) timePart = `at ${times.join(', ')}`
  else timePart = `at minute ${fieldTxt(M)} of hour ${fieldTxt(H)}`

  const segs = [timePart]
  if (!D.star) segs.push(`on ${D.list.length > 1 ? 'days' : 'day'} ${fieldTxt(D)}`)
  if (!MO.star) segs.push(`in ${MO.list.map((m) => MON_SHORT[m - 1]).join(', ')}`)
  else if (!D.star) segs.push('of every month')
  // "and on" where a day-of-month is also set: APScheduler ANDs the two fields
  if (!W.star) segs.push(`${D.star ? 'on' : 'and on'} ${W.list.map((d) => DOW_SHORT[d]).join(', ')}`)
  return segs.join(' ')
}

// Day by day rather than minute by minute: at most ~3000 iterations, which is
// the widest real gap (29 February can be eight years out).
function cronNext(M, H, D, MO, W, from) {
  const base = from instanceof Date ? from : new Date()
  if (Number.isNaN(base.getTime())) return null
  const floor = Math.floor(base.getTime() / 60000) * 60000 + 60000   // the next whole minute
  // walk APP_TZ calendar days; the UTC Date below only carries the y/m/d arithmetic
  const s = tzParts(new Date(floor))
  for (let i = 0; i < 3000; i++) {
    const day = new Date(Date.UTC(s.y, s.mo - 1, s.d + i))
    if (!MO.set.has(day.getUTCMonth() + 1)) continue
    if (!D.set.has(day.getUTCDate())) continue
    if (!W.set.has((day.getUTCDay() + 6) % 7)) continue             // JS 0=Sun → APScheduler 0=Mon
    for (const h of H.list) {
      for (const m of M.list) {
        const t = zonedInstant(day.getUTCFullYear(), day.getUTCMonth() + 1, day.getUTCDate(), h, m).getTime()
        if (t >= floor) return new Date(t)
      }
    }
  }
  return null
}

// The extras APScheduler really accepts through `CronTrigger.from_crontab` (what
// backend/seed.py `_cron_error` validates a saved value with) but this parser does
// not evaluate: `last` / `lastw` / `last-5` in the day field, and `fri#3` in
// day-of-week. Those are echoed rather than called invalid.
//
// Everything the old regex ALSO caught is refused by from_crontab, so it stays
// invalid on this side too — the field-level check is what keeps the two answers
// the same (R4-E2E-05). Verified against apscheduler 3.10.4 in the container:
//   `0 3 ? * *`         Unrecognized expression "?" for field "day"
//   `0 3 5w * *`        Unrecognized expression "5w" for field "day"
//   `0 3 * * last`      Invalid weekday name "last"      (`last` is day-of-MONTH)
//   `0 3 * * 4#2`       Unrecognized expression "4#2"    (`#` needs a weekday NAME)
//   `0 3 1st * *`       Unrecognized expression "1st"
// A SPACED extension (`0 3 * * last sun`) is six fields, and from_crontab rejects
// any count but five — so the five-field guard below returning null is the right
// answer for it, not an echo.
const CRON_DAY_EXTRA = /^last/i         // day of month: last, lastw, last-5
const CRON_DOW_EXTRA = /^[a-z]{3}#/i    // day of week: fri#3 — nth weekday of the month
const cronExtras = (day, dow) => day.split(',').some((t) => CRON_DAY_EXTRA.test(t.trim()))
                              || dow.split(',').some((t) => CRON_DOW_EXTRA.test(t.trim()))

export function describeCron(expr, opts = {}) {
  const raw = String(expr == null ? '' : expr).trim()
  if (!raw) return null
  const parts = raw.split(/\s+/)
  if (parts.length !== 5) return null
  // `unparsed` separates "we did not read this" from "this can never fire" —
  // both carry a null `next`, and the row must not call the first one dead
  if (cronExtras(parts[2], parts[4])) return { text: raw, next: null, unparsed: true }
  const M = parseField(parts[0], 0, 59)
  const H = parseField(parts[1], 0, 23)
  const D = parseField(parts[2], 1, 31)
  const MO = parseField(parts[3], 1, 12, MON_NAMES)
  const W = parseField(parts[4], 0, 6, DOW_NAMES)
  if (!M || !H || !D || !MO || !W) return null
  return { text: cronText(M, H, D, MO, W), next: cronNext(M, H, D, MO, W, opts.from) }
}

// Presets use names (mon-fri, mon) not digits: under APScheduler's numbering
// 1-5/1 mean Tue-Sat/Tuesday, so digits would mislabel the schedule (DECISIONS D-20).
export const CRON_PRESETS = [
  ['Hourly', '0 * * * *'],
  ['Every 6 hours', '0 */6 * * *'],
  ['Daily 03:00', '0 3 * * *'],
  ['Weekdays 09:00', '0 9 * * mon-fri'],
  ['Weekly Monday 08:00', '0 8 * * mon'],
  ['Monthly 1st 08:00', '0 8 1 * *'],
  ['Off', ''],
]

// ── describeCron, checked ───────────────────────────────────────────────────
// The table is asserted in src/__tests__/time.test.js. `next` there is APP_TZ
// (New York) wall-clock time; the scheduler runs in the same zone (backend/tz.py).
