// Node 22+ ships an experimental global `localStorage` that shadows jsdom's and has no
// working methods without --localstorage-file. Install a plain in-memory Storage in that case.
function memoryStorage() {
  let data = new Map()
  return {
    get length() { return data.size },
    key: (i) => Array.from(data.keys())[i] ?? null,
    getItem: (k) => (data.has(String(k)) ? data.get(String(k)) : null),
    setItem: (k, v) => { data.set(String(k), String(v)) },
    removeItem: (k) => { data.delete(String(k)) },
    clear: () => { data = new Map() },
  }
}
for (const name of ['localStorage', 'sessionStorage']) {
  let ok = false
  try { ok = typeof globalThis[name]?.clear === 'function' } catch { ok = false }
  if (!ok) Object.defineProperty(globalThis, name, { value: memoryStorage(), configurable: true, writable: true })
}
