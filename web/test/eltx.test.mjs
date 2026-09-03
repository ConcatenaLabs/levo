// The transaction reader, against a transaction levod's own builder made.
//
// Both sides have to agree about the same bytes, which is the only reason this
// reader exists: the buy panel used to check levod's JSON description of what
// it had built against levod's own quote, and a server that lied about one
// would lie about the other.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { outputsOf } from '../src/lib/eltx.js'

const fixture = JSON.parse(readFileSync('test/fixtures/buy.json', 'utf8'))

test('the bytes say what levod said they say', () => {
  const outs = outputsOf(fixture.hex)
  assert.equal(outs.length, fixture.outputs.length)
  fixture.outputs.forEach((said, i) => {
    assert.equal(outs[i].asset, said.asset, 'output ' + i + ' asset')
    assert.equal(outs[i].atoms, BigInt(said.atoms), 'output ' + i + ' amount')
    assert.equal(outs[i].script, said.script_pubkey, 'output ' + i + ' script')
  })
})

test('the covenant rule is readable from the bytes alone', () => {
  const outs = outputsOf(fixture.hex)
  // Output 0 pays the treasury in the payment asset; output 1 re-rests the
  // remainder at the sale's own address in the token. That is the whole of
  // what the sell leaf enforces, and it is checkable here without asking.
  assert.equal(outs[0].script, fixture.treasury_script_pubkey)
  assert.equal(outs[0].asset, fixture.payment_asset)
  assert.equal(outs[1].script, fixture.sale_script_pubkey)
  assert.equal(outs[1].asset, fixture.token_asset)
  const fee = outs.filter((o) => o.isFee)
  assert.equal(fee.length, 1, 'exactly one fee output')
})

test('it refuses what it cannot read rather than guessing', () => {
  assert.throws(() => outputsOf('not hex'), /not a transaction/)
  assert.throws(() => outputsOf('0200'), /ends mid-field/)
  // An output whose asset is a commitment rather than a statement: the tag
  // byte of output 0's asset (0x01 here) becomes 0x0a.
  const at = fixture.hex.indexOf('01deb9044d8fa54b')
  const blinded = fixture.hex.slice(0, at) + '0a' + fixture.hex.slice(at + 2)
  assert.throws(() => outputsOf(blinded), /hides its asset/)
})
