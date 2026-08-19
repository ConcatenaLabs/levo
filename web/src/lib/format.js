// Atoms are the only amount the chain understands, so they are what Levo sends
// and receives. These helpers exist to display them, never to compute with
// them: every arithmetic decision belongs to the backend and the covenant.

export const SEQ = 100000000
export const USDX = 100000000

export function fromAtoms(atoms, decimals = 8) {
  const n = Number(atoms || 0) / Math.pow(10, decimals)
  return n
}

export function amount(atoms, decimals = 8, maxFrac = 8) {
  const n = fromAtoms(atoms, decimals)
  return n.toLocaleString(undefined, { maximumFractionDigits: maxFrac })
}

// Large stake figures read better abbreviated, but never on a page where the
// exact number is the point -- pass exact there.
export function compact(atoms, decimals = 8) {
  const n = fromAtoms(atoms, decimals)
  if (n >= 1000000) return (n / 1000000).toLocaleString(undefined, { maximumFractionDigits: 2 }) + 'M'
  if (n >= 1000) return (n / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 }) + 'k'
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

export function toAtoms(text, decimals = 8) {
  const s = String(text ?? '').trim()
  if (!s) return null
  if (!/^\d*\.?\d*$/.test(s)) return null
  const [whole, frac = ''] = s.split('.')
  if (frac.length > decimals) return null
  return BigInt(whole || '0') * BigInt(Math.pow(10, decimals)) +
         BigInt((frac + '0'.repeat(decimals)).slice(0, decimals) || '0')
}

export function shortHex(h, head = 8, tail = 6) {
  if (!h) return ''
  const s = String(h)
  return s.length <= head + tail + 1 ? s : s.slice(0, head) + '…' + s.slice(-tail)
}

export function price(terms) {
  // payment atoms per token atom, shown per whole token
  if (!terms) return 0
  return (terms.price_num / terms.price_den)
}

export function closeLabel(locktime) {
  if (!locktime) return 'not set'
  if (locktime < 500000000) return 'block ' + locktime.toLocaleString()
  return new Date(locktime * 1000).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  })
}
