// The Levo API client. One origin, bearer token in localStorage.

const TOKEN_KEY = 'levo.session'
const listeners = new Set()

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) } catch { return null }
}
export function setToken(t) {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY) } catch {}
}
// Called whenever a request learns the session is gone, so the whole page
// can stop showing an account it no longer holds.
export function onSignedOut(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
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
  let res
  try {
    res = await fetch(API + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError('Levo could not reach its server. Check your connection and try again.', 0)
  }
  const ctype = res.headers.get('content-type') || ''
  const text = await res.text()
  let data = {}
  if (ctype.includes('application/json')) {
    try { data = text ? JSON.parse(text) : {} } catch { data = {} }
  } else if (res.status !== 204) {
    throw new ApiError('Levo’s server gave an unexpected answer (' + res.status + '). Try again in a moment.', res.status)
  }
  if (res.status === 401 && token) {
    setToken(null)
    listeners.forEach((fn) => fn())
  }
  if (!res.ok) throw new ApiError(data.error || ('request failed (' + res.status + ')'), res.status, data)
  return data
}

export const api = {
  health: () => call('GET', '/health'),
  config: () => call('GET', '/config'),
  tiers: () => call('GET', '/tiers'),
  rails: () => call('GET', '/rails'),
  me: () => call('GET', '/me'),
  myProjects: () => call('GET', '/me/projects'),
  myPositions: () => call('GET', '/me/positions'),

  authChallenge: () => call('POST', '/auth/challenge'),
  authVerify: (message, signature, address) =>
    call('POST', '/auth/verify', address ? { message, signature, address } : { message, signature }),

  stakeChallenge: (staker_pubkey) => call('POST', '/stake/challenge', { staker_pubkey }),
  stakeLink: (message, signature, staker_pubkey) =>
    call('POST', '/stake/link', { message, signature, staker_pubkey }),
  stakeUnlink: (staker_pubkey) => call('POST', '/stake/unlink', { staker_pubkey }),

  projects: () => call('GET', '/projects'),
  project: (slug) => call('GET', '/projects/' + slug),
  createProject: (project, terms) => call('POST', '/projects', { project, terms }),
  updateProject: (slug, meta) => call('PATCH', '/projects/' + slug, meta),
  withdraw: (slug) => call('DELETE', '/projects/' + slug),
  lock: (slug, txid, vout) =>
    call('POST', '/projects/' + slug + '/lock', txid ? { txid, vout } : {}),
  buy: (slug, payload) => call('POST', '/projects/' + slug + '/buy', payload),
  confirm: (slug, payload) => call('POST', '/projects/' + slug + '/confirm', payload),
  transaction: (slug, payload) => call('POST', '/projects/' + slug + '/transaction', payload),
  reclaim: (slug, payload) => call('POST', '/projects/' + slug + '/reclaim', payload),
  watcher: () => call('GET', '/watcher'),
}
