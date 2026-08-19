// The Levo API client. One origin, bearer token in memory + localStorage.

const TOKEN_KEY = 'levo.session'

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) } catch { return null }
}
export function setToken(t) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY) } catch {}
}

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message)
    this.status = status
    this.body = body || {}
  }
}

// Served from the site root in development and from /levo/ in production, so
// every request is built off the base the bundle was compiled with.
const API = (import.meta.env.BASE_URL || '/').replace(/\/$/, '') + '/api'

async function call(method, path, body) {
  const headers = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = 'Bearer ' + token
  const res = await fetch(API + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  let data = {}
  try { data = text ? JSON.parse(text) : {} } catch { data = { error: text } }
  if (!res.ok) throw new ApiError(data.error || ('request failed (' + res.status + ')'), res.status, data)
  return data
}

export const api = {
  health: () => call('GET', '/health'),
  tiers: () => call('GET', '/tiers'),
  rails: () => call('GET', '/rails'),
  me: () => call('GET', '/me'),

  authChallenge: () => call('POST', '/auth/challenge'),
  authVerify: (message, signature) => call('POST', '/auth/verify', { message, signature }),

  stakeChallenge: (staker_pubkey) => call('POST', '/stake/challenge', { staker_pubkey }),
  stakeLink: (message, signature, staker_pubkey) =>
    call('POST', '/stake/link', { message, signature, staker_pubkey }),
  stakeUnlink: (staker_pubkey) => call('POST', '/stake/unlink', { staker_pubkey }),

  projects: () => call('GET', '/projects'),
  project: (slug) => call('GET', '/projects/' + slug),
  createProject: (project, terms) => call('POST', '/projects', { project, terms }),
  lock: (slug, txid, vout) => call('POST', '/projects/' + slug + '/lock', { txid, vout }),
  buy: (slug, payload) => call('POST', '/projects/' + slug + '/buy', payload),
  confirm: (slug, payload) => call('POST', '/projects/' + slug + '/confirm', payload),
  transaction: (slug, payload) => call('POST', '/projects/' + slug + '/transaction', payload),
  reclaim: (slug, payload) => call('POST', '/projects/' + slug + '/reclaim', payload),
  watcher: () => call('GET', '/watcher'),
}
