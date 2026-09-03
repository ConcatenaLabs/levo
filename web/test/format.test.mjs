import { test } from 'node:test'
import assert from 'node:assert/strict'
import { amount, compact, toAtoms, atomsArg, closeLabel, closeIn, isHeightClose, pricePerToken, treasurySpk, plain,
         capitalise, big, prose } from '../src/lib/format.js'
import { xFor, yFor } from '../src/lib/beam.js'
import { addressOf, encodeSegwit } from '../src/lib/bech32.js'

test('amount formats atoms exactly, above 2**53 too', () => {
  assert.equal(amount(123450000000n), '1,234.5')
  assert.equal(amount('99999999999999999'), '999,999,999.99999999')
  assert.equal(amount(100000000n, 8), '1')
  assert.equal(amount(5n, 0), '5')
  assert.equal(amount(1234n, 2), '12.34')
  assert.equal(amount(-250000000n), '-2.5')
  assert.equal(amount(123456789n, 8, 2), '1.23')
})

test('toAtoms reads what a person types', () => {
  assert.equal(toAtoms('12.5'), 1250000000n)
  assert.equal(toAtoms('1,000'), 100000000000n)
  assert.equal(toAtoms('.5'), 50000000n)
  assert.equal(toAtoms('1.'), 100000000n)
  assert.equal(toAtoms('1.123456789'), null)
  assert.equal(toAtoms('-1'), null)
  assert.equal(toAtoms('1e5'), null)
  assert.equal(toAtoms(''), null)
  assert.equal(toAtoms('3', 0), 3n)
  assert.equal(toAtoms('3.1', 0), null)
  assert.equal(atomsArg(toAtoms('90071992.54740993')), '9007199254740993')
})

test('compact abbreviates', () => {
  assert.equal(compact(4000000000000n), '40k')
  assert.equal(compact(100000000000000n), '1M')
  assert.equal(compact(99995000000000n), '1,000k')
  assert.equal(compact(150000000n), '1.5')
})

test('closes: heights below 500,000,000, times above, shown in UTC', () => {
  assert.equal(isHeightClose(499999999), true)
  assert.equal(isHeightClose(500000000), false)
  assert.equal(closeLabel(120000), 'block 120,000')
  assert.equal(closeLabel(1792307521), 'Oct 18 2026, 07:12 UTC')
  assert.equal(closeIn(120000, 118600), 'in about 1,400 blocks (23 hours)')
  assert.equal(closeIn(120000, 118560), 'in about 1,440 blocks (1 day)')
  assert.equal(closeIn(120000, 120000), 'closed')
  assert.equal(closeIn(120000, null), '')
  assert.equal(closeIn(1792307521, null, 1792307521 - 2 * 86400), 'in 2 days')
  assert.equal(closeIn(1792307521, null, 1792307521 + 1), 'closed')
})

test('price per token scales by the token decimals', () => {
  assert.equal(pricePerToken({ price_num: 1, price_den: 4 }, 8), 0.25)
  assert.equal(pricePerToken({ price_num: 1, price_den: 4 }, 2), 0.25e-6)
  assert.equal(pricePerToken({ price_num: 25, price_den: 100 }, 8), 0.25)
})

test('capitalise and big', () => {
  assert.equal(capitalise('the minimum purchase is 10 HLX'), 'The minimum purchase is 10 HLX')
  assert.equal(big('12'), 12n)
  assert.equal(big(undefined), 0n)
  assert.equal(big(3.7), 4n)
})

test('beam geometry: thresholds, the top tier, and equal stops', () => {
  const stops = [0, 40000, 200000, 1000000]
  assert.equal(xFor(0, stops), 0)
  assert.equal(xFor(40000, stops), 0.25)
  assert.equal(xFor(120000, stops), 0.375)
  assert.equal(xFor(1000000, stops), 0.75)
  assert.ok(xFor(10000000, stops) <= 0.75 + 0.85 / 4 + 1e-9)
  assert.equal(xFor(5, [0, 0, 10]), 0.5)
  assert.equal(yFor(0, 100, 30, 150), 150)
  assert.equal(yFor(100, 100, 30, 150), 30)
  assert.ok(yFor(25, 100, 30, 150) < 150 && yFor(25, 100, 30, 150) > 30)
})

test('bech32m encodes a taproot program and bech32 a v0 one', () => {
  assert.equal(addressOf('512096ec3dd929d60f1bdaca389a5461e2b044dc1c41561bcd92e3897327781c96a1', 'tb'),
               'tb1pjmkrmkff6c83hkk28zd9gc0zkpzdc8zp2cdumyhr39ejw7quj6ssw5ug74')
  assert.equal(encodeSegwit('bc', 0, '751e76e8199196d454941c45d1b3a323f1433bd6'),
               'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4')
  assert.equal(encodeSegwit('bc', 1, '79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798'),
               'bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0')
  assert.equal(addressOf('6a04deadbeef'), null)
})

test('a treasury scriptPubKey follows the version in the terms', () => {
  assert.equal(treasurySpk({ treasury_prog: 'aa'.repeat(32) }), '5120' + 'aa'.repeat(32))
  assert.equal(treasurySpk({ treasury_prog: 'bb'.repeat(32), treasury_ver: 1 }), '5120' + 'bb'.repeat(32))
  assert.equal(treasurySpk({ treasury_prog: 'cc'.repeat(20), treasury_ver: 0 }), '0014' + 'cc'.repeat(20))
  assert.equal(treasurySpk(null), '')
})

test('a close says the time of day, not just the date', () => {
  const label = closeLabel(2000000000)
  assert.match(label, /\d\d:\d\d UTC$/)
  assert.equal(closeLabel(900000), 'block 900,000')
})

test('a version-0 treasury script says how long its program is', () => {
  assert.equal(treasurySpk({ treasury_prog: 'bb'.repeat(20), treasury_ver: 0 }), '0014' + 'bb'.repeat(20))
  assert.equal(treasurySpk({ treasury_prog: 'cc'.repeat(32), treasury_ver: 0 }), '0020' + 'cc'.repeat(32))
})

test('plain drops the separators a command cannot take', () => {
  assert.equal(plain(20000000000000n, 8), '200000')
  assert.equal(plain(1n, 8), '0.00000001')
  assert.equal(plain(123456789012345678n, 8), '1234567890.12345678')
})

test('one of something is never "1 minutes"', () => {
  assert.equal(closeIn(120000, 119999), 'in about 1 block (1 minute)')
  assert.equal(closeIn(120000, 119998), 'in about 2 blocks (2 minutes)')
  assert.equal(closeIn(1792307521, null, 1792307521 - 60), 'in 1 minute')
  assert.equal(closeIn(1792307521, null, 1792307521 - 3600), 'in 1 hour')
  assert.equal(closeIn(1792307521, null, 1792307521 - 86400), 'in 1 day')
})

test('server prose gets the site dash', () => {
  assert.equal(prose('gone -- and not coming back'), 'gone — and not coming back')
  assert.equal(capitalise('no output at ab:1 -- it is spent'),
    'No output at ab:1 — it is spent')
  assert.equal(prose('levo rescue --terms sale.json'), 'levo rescue --terms sale.json')
})
