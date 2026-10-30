// Every amount on Levo reads in BTC, or in US dollars where the viewer flips
// the switch. Both come from the node's fee exchange-rate table (/api/rates):
// a rate is reference units -- a hundred-millionth of a dollar -- per 10^8
// atoms at any precision, so
//
//   satoshis = atoms * rate / btc_rate      cents = atoms * rate / 10^14
//
// The figures are for reading. What a buyer pays is the sale's own asset, and
// the purchase is built from atoms by levod, never from these.

import { amount, big } from './format.js'

export const UNITS = ['btc', 'usd']
const KEY = 'levo.unit'

export function savedUnit() {
  try {
    const u = window.localStorage.getItem(KEY)
    return UNITS.includes(u) ? u : 'btc'
  } catch (e) {
    return 'btc'
  }
}

export function saveUnit(u) {
  try { window.localStorage.setItem(KEY, u) } catch (e) { /* a private window keeps it for the visit */ }
}

// BTC the way the wallets write it: up to eight places, trailing zeros
// dropped, the unit after.
export function btcText(sats) {
  return amount(sats, 8) + ' BTC'
}

export function usdText(cents) {
  const c = big(cents)
  const abs = c < 0n ? -c : c
  const whole = (abs / 100n).toLocaleString('en-US')
  const frac = (abs % 100n).toString().padStart(2, '0')
  return (c < 0n ? '-$' : '$') + whole + '.' + frac
}

// An amount of `asset` in the viewer's unit, or null when the node prices
// neither it nor bitcoin (the caller then shows the asset's own units).
export function convert(atoms, asset, rates, unit) {
  if (!rates || !rates.rates) return null
  const a = String(asset || '').toLowerCase()
  if (a === 'btc') {
    if (unit === 'btc') return btcText(atoms)
    if (!rates.btc_rate) return null
    return usdText(big(atoms) * big(rates.btc_rate) / 10n ** 14n)
  }
  const rate = rates.rates[a]
  if (!rate) return null
  if (unit === 'usd') return usdText(big(atoms) * big(rate) / 10n ** 14n)
  if (!rates.btc_rate) return null
  return btcText(big(atoms) * big(rate) / big(rates.btc_rate))
}

// The formatter a page uses: the viewer's unit where the rates allow, the
// asset's own units where they do not, so a figure is never left blank.
export function moneyText(atoms, asset, rates, unit, fallback = {}) {
  const shown = convert(atoms, asset, rates, unit)
  if (shown) return shown
  const { label = '', decimals = 8 } = fallback
  return amount(atoms, decimals) + (label ? ' ' + label : '')
}

// A price per whole token, in the viewer's unit: the covenant prices atoms
// per atom, so a whole token costs 10^tokenDecimals * num / den atoms.
export function priceText(terms, tokenDecimals, asset, rates, unit, fallback = {}) {
  if (!terms) return ''
  const perToken = big(terms.price_num) * 10n ** BigInt(tokenDecimals) / big(terms.price_den || 1)
  return moneyText(perToken, asset, rates, unit, fallback)
}
