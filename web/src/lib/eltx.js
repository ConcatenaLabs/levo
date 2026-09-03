// Reading the transaction a buyer is about to sign.
//
// Everything the buy panel checked before signing came out of levod's own JSON
// description of what it had built: the outputs it listed, compared with the
// plan it had quoted. Both halves are the same server's word, so a levod that
// lied about one would lie about the other, and the page whose whole claim is
// that you do not have to take Levo's word for anything took it here.
//
// So the bytes are read instead. This parses just enough of an Elements
// transaction to see its outputs -- asset, amount, script -- which is what the
// covenant's own rule is written in terms of. It is deliberately strict: an
// output that is blinded, or a field shaped in a way this does not understand,
// is an error rather than a guess, because the only use of this is to refuse.

class Reader {
  constructor(hex) {
    if (typeof hex !== 'string' || !/^[0-9a-fA-F]*$/.test(hex) || hex.length % 2) {
      throw new Error('that is not a transaction in hex')
    }
    this.b = new Uint8Array(hex.length / 2)
    for (let i = 0; i < this.b.length; i++) this.b[i] = parseInt(hex.substr(i * 2, 2), 16)
    this.at = 0
  }

  need(n) {
    if (this.at + n > this.b.length) throw new Error('the transaction ends mid-field')
    const out = this.b.subarray(this.at, this.at + n)
    this.at += n
    return out
  }

  u8() { return this.need(1)[0] }

  u32() {
    const v = this.need(4)
    return (v[0] | (v[1] << 8) | (v[2] << 16)) + v[3] * 0x1000000
  }

  // A compact-size integer. Sizes here are counts of inputs, outputs and
  // script bytes, so anything past 2**32 is a malformed transaction, not a
  // number this has to carry.
  compact() {
    const n = this.u8()
    if (n < 0xfd) return n
    if (n === 0xfd) { const v = this.need(2); return v[0] | (v[1] << 8) }
    if (n === 0xfe) return this.u32()
    const v = this.need(8)
    for (let i = 4; i < 8; i++) if (v[i]) throw new Error('a length this transaction claims is absurd')
    return (v[0] | (v[1] << 8) | (v[2] << 16)) + v[3] * 0x1000000
  }

  hex(n) {
    const v = this.need(n)
    let s = ''
    for (const byte of v) s += byte.toString(16).padStart(2, '0')
    return s
  }

  // An explicit asset: 0x01 followed by the id in WIRE order, which is the
  // reverse of the order people read it in.
  asset() {
    const tag = this.u8()
    if (tag !== 1) throw new Error('an output in this transaction hides its asset')
    const wire = this.need(32)
    let s = ''
    for (let i = 31; i >= 0; i--) s += wire[i].toString(16).padStart(2, '0')
    return s
  }

  // An explicit value: 0x01 followed by eight bytes, big-endian -- unlike
  // almost everything else in the format.
  value() {
    const tag = this.u8()
    if (tag !== 1) throw new Error('an output in this transaction hides its amount')
    const v = this.need(8)
    let n = 0n
    for (const byte of v) n = (n << 8n) | BigInt(byte)
    return n
  }

  nonce() {
    const tag = this.u8()
    if (tag === 0) return null
    if (tag === 1) { this.need(32); return 'explicit' }
    this.need(32)                       // a commitment: 0x02/0x03 and 32 bytes
    return 'committed'
  }
}

// The outputs of an unsigned Elements transaction, in order:
// [{index, asset, atoms, script, isFee}]. Throws on anything it cannot read.
export function outputsOf(hex) {
  const r = new Reader(hex)
  r.u32()                               // version
  const flag = r.u8()                   // Elements: 0 or 1, then the inputs
  const nin = r.compact()
  if (nin < 1) throw new Error('a transaction with no inputs')
  for (let i = 0; i < nin; i++) {
    r.need(32)                          // previous txid
    r.u32()                             // previous vout
    r.need(r.compact())                 // scriptSig
    r.u32()                             // sequence
  }
  const nout = r.compact()
  const outs = []
  for (let i = 0; i < nout; i++) {
    const asset = r.asset()
    const atoms = r.value()
    const nonce = r.nonce()
    const script = r.hex(r.compact())
    if (nonce === 'committed') throw new Error('an output in this transaction is blinded')
    outs.push({ index: i, asset, atoms, script, isFee: script.length === 0 })
  }
  r.u32()                               // locktime
  if (flag !== 0 && flag !== 1) throw new Error('this is not an Elements transaction')
  return outs
}
