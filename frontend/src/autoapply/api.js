import api from '../api'

// Thin wrappers over /api/autoapply/*, so screens never spell out paths.
export const getSummary = (runDate) => api.get('/autoapply/summary', { params: { run_date: runDate } }).then((r) => r.data)
export const getItems = (params) => api.get('/autoapply/items', { params }).then((r) => r.data)
export const getItem = (id) => api.get(`/autoapply/items/${id}`).then((r) => r.data)
export const reprepareItem = (id) => api.post(`/autoapply/items/${id}/prepare`).then((r) => r.data)
export const getQuestions = (params) => api.get('/autoapply/questions', { params }).then((r) => r.data)
export const answerQuestion = (id, answer) => api.post(`/autoapply/questions/${id}/answer`, { answer, save_to_bank: true }).then((r) => r.data)
export const getStats = (days) => api.get('/autoapply/stats', { params: { days } }).then((r) => r.data)
export const cdpStatus = () => api.get('/autoapply/cdp/status').then((r) => r.data)
export const resumeSite = (site) => api.post(`/autoapply/sites/${site}/resume`).then((r) => r.data)
export const itemAction = (action, ids) => api.post('/autoapply/items/action', { action, ids }).then((r) => r.data)
export const rebuildQueue = () => api.post('/autoapply/queue/rebuild').then((r) => r.data)
export const getSettings = () => api.get('/autoapply/settings').then((r) => r.data)
export const putSettings = (values) => api.put('/autoapply/settings', values).then((r) => r.data)

// FastAPI sends `detail` as a string, an array of {msg}, or (our settings 422) a {key: reason} map.
export const errText = (e, fallback) => {
  const d = e?.response?.data?.detail
  if (typeof d === 'string' && d) return d
  if (Array.isArray(d) && d.length) return d.map((x) => x?.msg || JSON.stringify(x)).join('; ')
  if (d && typeof d === 'object') return Object.entries(d).map(([k, v]) => `${k}: ${v}`).join('; ')
  return fallback
}
