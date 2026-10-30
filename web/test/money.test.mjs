import test from 'node:test'
import assert from 'node:assert/strict'
import { convert, moneyText as money, btcText, usdText, priceText as priceMoney } from '../src/lib/money.js'

const USDX = 'aa'.repeat(32)
// USDX at a dollar (rate 1e8), bitcoin at $80,000 (rate 8e12).
const rates = { rates: { [USDX]: 100000000 }, btc_rate: 8000000000000 }

test('a dollar of USDX is 1,250 sats', () => {
  assert.equal(convert(100000000n, USDX, rates, 'btc'), '0.0000125 BTC')
  assert.equal(convert(100000000n, USDX, rates, 'usd'), '$1.00')
})

test('bitcoin itself reads in BTC and in dollars', () => {
  assert.equal(convert(100000000n, 'btc', rates, 'btc'), '1 BTC')
  assert.equal(convert(12000n, 'btc', rates, 'usd'), '$9.60')
})

test('an unpriced asset falls back to its own units', () => {
  assert.equal(money(150000000n, 'bb'.repeat(32), rates, 'btc', { label: 'X', decimals: 8 }), '1.5 X')
})

test('formats', () => {
  assert.equal(btcText(87n), '0.00000087 BTC')
  assert.equal(usdText(123456n), '$1,234.56')
  assert.equal(usdText(5n), '$0.05')
  assert.equal(usdText(10n), '$0.10')
})

test('a price per whole token', () => {
  // 1/4 USDX atom per token atom: a whole token is 0.25 USDX = $0.25
  assert.equal(priceMoney({ price_num: 1, price_den: 4 }, 8, USDX, rates, 'usd'), '$0.25')
})
