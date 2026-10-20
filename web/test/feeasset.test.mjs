// The fee asset rule every wallet uses, as the capped purchase applies it when
// the node does not take fees in the asset the sale is paid in.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { pickFeeAsset } from '../src/lib/feeasset.js'

const A = 'a'.repeat(64), B = 'b'.repeat(64), C = 'c'.repeat(64)

test('the accepted asset whose largest coin covers the most fees', () => {
  const rates = { [A]: '1000', [B]: '10' }
  // B: a coin of 5,000 atoms covers 500 kvB; A: 100,000 atoms covers 100 kvB.
  const utxos = [{ asset: A, value: '100000' }, { asset: B, value: '5000' }, { asset: B, value: '200' }]
  assert.equal(pickFeeAsset(rates, utxos), B)
})

test('the largest coin, not the sum', () => {
  const rates = { [A]: '10', [B]: '10' }
  const utxos = [{ asset: A, value: '600' }, { asset: A, value: '600' }, { asset: B, value: '1000' }]
  assert.equal(pickFeeAsset(rates, utxos), B)
})

test('an asset the node does not accept is never chosen, and none held is null', () => {
  assert.equal(pickFeeAsset({ [A]: '10' }, [{ asset: C, value: '999999' }]), null)
  assert.equal(pickFeeAsset({ [A]: '10', [C]: '0' }, [{ asset: C, value: '999999' }, { asset: A, value: '20' }]), A)
})

test('a tie goes to the lower id, which says nothing about either asset', () => {
  assert.equal(pickFeeAsset({ [B]: '10', [A]: '10' }, [{ asset: B, value: '100' }, { asset: A, value: '100' }]), A)
})
