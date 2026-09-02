// bech32 and bech32m, for turning a witness program into the address a wallet
// shows. Levo's backend does the checking; this exists so a page can print the
// treasury as an address rather than as 64 hex characters.

const CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
const BECH32M = 0x2bc830a3

function polymod(values) {
  const gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
  let chk = 1
  for (const v of values) {
    const top = chk >>> 25
    chk = ((chk & 0x1ffffff) << 5) ^ v
    for (let i = 0; i < 5; i++) if ((top >>> i) & 1) chk ^= gen[i]
  }
  return chk >>> 0
}

function hrpExpand(hrp) {
  const out = []
  for (const c of hrp) out.push(c.charCodeAt(0) >> 5)
  out.push(0)
  for (const c of hrp) out.push(c.charCodeAt(0) & 31)
  return out
}

function convertBits(data, from, to) {
  let acc = 0, bits = 0
  const out = []
  const maxv = (1 << to) - 1
  for (const v of data) {
    acc = (acc << from) | v
    bits += from
    while (bits >= to) { bits -= to; out.push((acc >> bits) & maxv) }
  }
  if (bits) out.push((acc << (to - bits)) & maxv)
  return out
}

export function encodeSegwit(hrp, witver, programHex) {
  const prog = []
  for (let i = 0; i < programHex.length; i += 2) prog.push(parseInt(programHex.substr(i, 2), 16))
  const data = [witver, ...convertBits(prog, 8, 5)]
  const constant = witver === 0 ? 1 : BECH32M
  const values = [...hrpExpand(hrp), ...data]
  const pm = polymod([...values, 0, 0, 0, 0, 0, 0]) ^ constant
  const checksum = []
  for (let i = 0; i < 6; i++) checksum.push((pm >>> (5 * (5 - i))) & 31)
  return hrp + '1' + [...data, ...checksum].map((d) => CHARSET[d]).join('')
}

// The address for a scriptPubKey such as 5120<32 bytes> or 0014<20 bytes>.
export function addressOf(spkHex, hrp = 'tb') {
  const s = String(spkHex || '').toLowerCase()
  if (s.length < 8) return null
  const ver = parseInt(s.slice(0, 2), 16)
  const len = parseInt(s.slice(2, 4), 16)
  const witver = ver === 0 ? 0 : (ver >= 0x51 && ver <= 0x60 ? ver - 0x50 : null)
  if (witver === null || s.length !== 4 + 2 * len) return null
  return encodeSegwit(hrp, witver, s.slice(4))
}
