// The build's own preflight, which is the only thing standing between an old
// Node and a deploy that reports success while serving the previous bundle.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { satisfies } from '../scripts/check-node.mjs'

const VITE = '^20.19.0 || >=22.12.0'

test('the range vite states is read the way vite means it', () => {
  assert.equal(satisfies('v18.20.8', VITE), false, 'the version the box had')
  assert.equal(satisfies('v20.18.0', VITE), false, '20, but below the patch line')
  assert.equal(satisfies('v20.19.0', VITE), true, 'the first 20 that works')
  assert.equal(satisfies('v20.30.0', VITE), true, 'later in the same major')
  assert.equal(satisfies('v21.7.3', VITE), false, '21 is in neither clause')
  assert.equal(satisfies('v22.11.0', VITE), false, '22, but below the patch line')
  assert.equal(satisfies('v22.12.0', VITE), true, 'the first 22 that works')
  assert.equal(satisfies('v24.18.0', VITE), true, 'the Node the box keeps for this')
})

test('an unreadable range is not a refusal', () => {
  // A build blocked by a range this could not parse would be worse than the
  // failure it exists to explain.
  assert.equal(satisfies('v18.0.0', ''), true, 'no range: nothing to check against')
  assert.equal(satisfies('v18.0.0', 'whatever npm invents next'), true, 'nor an unknown shape')
})
