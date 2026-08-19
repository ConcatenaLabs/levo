// Talking to the user's wallet.
//
// Levo asks a wallet for exactly one thing: a signature over a challenge it can
// read. It never asks for a private key, a seed, or permission to spend, and
// there is no code path here that could use one if it were offered.
//
// The browser extension exposes `window.sequentia`. Where it is absent the app
// falls back to asking the user to sign the same text with any wallet they have
// -- `sequentia-cli signmessage`, a hardware wallet, another app -- and paste
// the signature back. The fallback is not a downgrade: it is the same signature
// over the same bytes, verified the same way.

export function provider() {
  return typeof window !== 'undefined' ? window.sequentia : undefined
}

export function hasProvider() {
  return !!provider()
}

async function request(method, params) {
  const p = provider()
  if (!p) throw new Error('no Sequentia wallet found in this browser')
  if (typeof p.request === 'function') return p.request({ method, params })
  if (typeof p[method] === 'function') return p[method](params)
  throw new Error('this wallet does not support ' + method)
}

export async function connect() {
  try { return await request('connect', {}) } catch (e) { throw e }
}

export async function signMessage(message) {
  const r = await request('signMessage', { message })
  const sig = typeof r === 'string' ? r : r && r.signature
  if (!sig) throw new Error('the wallet returned no signature')
  return sig
}
