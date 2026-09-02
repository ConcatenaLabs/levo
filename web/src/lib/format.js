// Atoms are the only amount the chain understands, so they are what Levo sends
// and receives. These helpers exist to display them, never to compute with
// them: every arithmetic decision belongs to the backend and the covenant.
//
// Amounts are carried as BigInt or as decimal strings. A JavaScript number
// cannot hold an atom count above 2**53, and a sale can be larger than that.

export function big(v) {
  if (typeof v === 'bigint') return v
  if (v === null || v === undefined || v === '') return 0n
  if (typeof v === 'number') return BigInt(Math.round(v))
  const s = String(v).trim()
  return /^-?\d+$/.test(s) ? BigInt(s) : 0n
}

// Exact formatting of an atom count in the asset's own units, thousands
// grouped, trailing zeros dropped, at most `maxFrac` decimals shown.
export function amount(atoms, decimals = 8, maxFrac = 8) {
  const n = big(atoms)
  const neg = n < 0n
  const abs = neg ? -n : n
  const base = 10n ** BigInt(decimals)
  const whole = abs / base
  let frac = (abs % base).toString().padStart(decimals, '0')
  frac = frac.slice(0, Math.min(decimals, maxFrac)).replace(/0+$/, '')
  const wholeText = whole.toLocaleString('en-US')
  return (neg ? '-' : '') + wholeText + (frac ? '.' + frac : '')
}

// Large stake figures read better abbreviated, but never on a page where the
// exact number is the point -- pass exact there.
export function compact(atoms, decimals = 8) {
  const n = Number(big(atoms)) / Math.pow(10, decimals)
  if (n >= 1000000) return (n / 1000000).toLocaleString('en-US', { maximumFractionDigits: 2 }) + 'M'
  if (n >= 1000) return (n / 1000).toLocaleString('en-US', { maximumFractionDigits: 1 }) + 'k'
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 })
}

// A decimal string typed by a person, to atoms. null when it is not a number
// or carries more decimals than the asset has.
export function toAtoms(text, decimals = 8) {
  const s = String(text ?? '').trim().replace(/,/g, '')
  if (!s) return null
  if (!/^\d*\.?\d*$/.test(s) || s === '.') return null
  const [whole, frac = ''] = s.split('.')
  if (frac.length > decimals) return null
  return BigInt(whole || '0') * (10n ** BigInt(decimals)) +
         BigInt((frac + '0'.repeat(decimals)).slice(0, decimals) || '0')
}

// What goes over the wire: a decimal string, which levod reads exactly.
export function atomsArg(v) {
  return big(v).toString()
}

export function shortHex(h, head = 8, tail = 6) {
  if (!h) return ''
  const s = String(h)
  return s.length <= head + tail + 1 ? s : s.slice(0, head) + '…' + s.slice(-tail)
}

// Payment-asset units per whole token. The covenant prices in atoms per atom;
// this scales by the token's decimals for display only.
export function pricePerToken(terms, tokenDecimals = 8, paymentDecimals = 8) {
  if (!terms) return 0
  return (Number(terms.price_num) / Number(terms.price_den)) *
         Math.pow(10, tokenDecimals) / Math.pow(10, paymentDecimals)
}

export function priceLabel(terms, tokenDecimals = 8) {
  const p = pricePerToken(terms, tokenDecimals)
  return p.toLocaleString('en-US', { maximumFractionDigits: 8 })
}

// A close is a block height below 500,000,000 and a unix time above it, the
// same rule the chain applies. Times are shown in UTC, which is the zone the
// issuer chose it in.
export function isHeightClose(locktime) {
  return Number(locktime) < 500000000
}

export function closeLabel(locktime) {
  if (!locktime) return 'not set'
  if (isHeightClose(locktime)) return 'block ' + Number(locktime).toLocaleString('en-US')
  // A close is a moment, not a day: a sale closing at 00:30 and one closing at
  // 23:30 are a day apart and would otherwise read the same.
  return new Date(Number(locktime) * 1000).toLocaleString('en-US', {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC',
  }).replace(',', '') + ' UTC'
}

// How far away the close is: "in 46 days", "in about 1,400 blocks (a day)".
export function closeIn(locktime, height, now = Date.now() / 1000) {
  if (!locktime) return ''
  if (isHeightClose(locktime)) {
    if (height === null || height === undefined) return ''
    const blocks = Number(locktime) - Number(height)
    if (blocks <= 0) return 'closed'
    return 'in about ' + blocks.toLocaleString('en-US') + ' blocks (' + duration(blocks * 60) + ')'
  }
  const secs = Number(locktime) - now
  if (secs <= 0) return 'closed'
  return 'in ' + duration(secs)
}

export function duration(seconds) {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + ' minutes'
  if (s < 86400) { const h = Math.round(s / 3600); return h + (h === 1 ? ' hour' : ' hours') }
  const d = Math.round(s / 86400)
  return d + (d === 1 ? ' day' : ' days')
}

export function capitalise(s) {
  const t = String(s || '')
  return t.charAt(0).toUpperCase() + t.slice(1)
}

export function timeLabel(unix) {
  return new Date(Number(unix) * 1000).toLocaleString('en-US', {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
  }) + ' UTC'
}

// The scriptPubKey a sale's treasury credit pays: taproot, or the version-0
// output most wallets hand out. The version is part of the terms, because it
// is part of what the covenant was compiled from.
export function treasurySpk(terms) {
  if (!terms || !terms.treasury_prog) return ''
  return (Number(terms.treasury_ver ?? 1) === 1 ? '5120' : '0014') + terms.treasury_prog
}

// What a tier lets you do, from the tier itself. An operator's blurb is
// flavour on top; the figures and the units come from the deployment, so a
// card can never quote a ticker the chain does not use.
export function tierSays(tier, paymentLabel) {
  if (!tier) return ''
  const cap = big(tier.cap_atoms)
  const lines = cap > 0n
    ? ['Up to ' + amount(cap, 8) + ' ' + paymentLabel + ' in any one sale.']
    : ['This tier cannot buy.']
  if (tier.may_list) lines.push('May list a project.')
  return lines.join(' ')
}
