import { big } from './format.js'

// The fee asset when the node does not take fees in the asset the sale is paid
// in: the accepted asset whose largest coin in this wallet covers the most
// fees, measured in thousands of vbytes at the node's rate. No asset is
// preferred for what it is; a tie goes to the lower asset id.
export function pickFeeAsset(rates, utxos) {
  let best = null
  let bestCover = -1n
  const ids = Object.keys(rates || {}).sort()
  for (const asset of ids) {
    const rate = big(rates[asset])
    if (rate <= 0n) continue
    let largest = 0n
    for (const u of utxos || []) {
      if (u.asset === asset && big(u.value) > largest) largest = big(u.value)
    }
    if (largest <= 0n) continue
    const cover = largest / rate
    if (cover > bestCover) { best = asset; bestCover = cover }
  }
  return best
}

