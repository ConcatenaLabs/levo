// Talking to the user's wallet.
//
// Levo asks a wallet for four things: a signature over a challenge it can
// read, a look at the outputs and address the wallet holds, a signature on
// the buyer's own inputs of a transaction Levo built, and the relay of that
// transaction. It never asks for a private key, a seed, or permission to
// spend on its own, and there is no code path here that could use one.
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

// The extension's own error strings, said in plain words.
export function friendly(err) {
  const m = String((err && err.message) || err || '')
  if (/locked/i.test(m)) return 'Your wallet is locked. Unlock it and try again.'
  if (/not connected|call connect/i.test(m)) return 'This site is not connected to your wallet yet. Approve the connection and try again.'
  if (/rejected/i.test(m)) return 'The request was declined in the wallet.'
  if (/timed out/i.test(m)) return 'The wallet did not answer in time. Try again.'
  if (/no Sequentia wallet/i.test(m)) return 'No Sequentia wallet was found in this browser.'
  return m
}

async function request(method, params) {
  const p = provider()
  if (!p) throw new Error('no Sequentia wallet found in this browser')
  if (typeof p.request === 'function') return p.request({ method, params })
  if (typeof p[method] === 'function') return p[method](params)
  throw new Error('this wallet does not support ' + method)
}

// Connecting doubles as the unlock prompt: a wallet that is connected but
// locked opens its unlock window here rather than failing later.
export async function connect() {
  return request('connect', {})
}

export async function getUtxos(params) {
  await connect()
  const r = await request('getUtxos', params || {})
  return (r && (r.utxos || r)) || []
}

export async function getAddress(params) {
  await connect()
  const r = await request('getAddress', params || {})
  return (r && r.address) || r
}

// Relays an already-signed transaction, given as hex.
export async function broadcastHex(hex) {
  await connect()
  const r = await request('broadcast', { hex })
  return (r && r.txid) || r
}

// Signs the buyer's own inputs of a PSET. The covenant input carries its
// witness already, so the wallet has nothing to sign there and cannot alter it.
export async function signPset(pset) {
  await connect()
  const r = await request('signPset', { pset })
  const signed = (r && r.pset) || r
  if (!signed || typeof signed !== 'string') throw new Error('the wallet returned no signed transaction')
  return signed
}

// Finalises a signed PSET and relays it.
export async function broadcastPset(pset) {
  const r = await request('broadcast', { pset })
  const txid = (r && r.txid) || r
  if (!txid || typeof txid !== 'string') throw new Error('the wallet returned no transaction id')
  return txid
}

export async function supportsPset() {
  try {
    const caps = await request('getCapabilities', {})
    const list = (caps && (caps.methods || caps)) || []
    return Array.isArray(list) && list.includes('signPset') && list.includes('broadcast')
  } catch { return false }
}

// A signature under the wallet's STAKING key (m/2/0), which is the key a stake
// is bonded to. `signMessage` signs with the master key, which is a different
// key and says nothing about a stake -- so signing in with this one is what
// makes a staker's tier appear without any further step.
export async function signStakerMessage(message) {
  const r = await request('signStakerMessage', { message })
  if (!r || !r.signature) throw new Error('the wallet returned no signature')
  return r
}

export async function getStakerPublicKey() {
  await connect()
  const r = await request('getStakerPublicKey', {})
  const pk = (r && r.staker_pubkey) || r
  if (!pk || typeof pk !== 'string') throw new Error('the wallet has no staking key')
  return pk.toLowerCase()
}

export async function supportsStakerSigning() {
  try {
    const caps = await request('getCapabilities', {})
    const list = (caps && (caps.methods || caps)) || []
    return Array.isArray(list) && list.includes('signStakerMessage')
  } catch { return false }
}

// Whether an error means "this wallet has no staking key", as opposed to the
// user declining or the wallet failing.
export function noStakingKey(err) {
  return /no Sequentia staking key|cannot sign with the staking key|does not support signStakerMessage/i
    .test(String((err && err.message) || err || ''))
}

export async function signMessage(message) {
  const r = await request('signMessage', { message })
  const sig = typeof r === 'string' ? r : r && r.signature
  if (!sig) throw new Error('the wallet returned no signature')
  return sig
}
