# Delegated stake counts, and counts for its owner

Sequentia lets a staker lend its block-signing rights to a pool without moving
the coins. A delegation record in the UTXO set points a **controller** -- the key
the stake is bonded to, and the only key that can ever spend it -- at a
**signer**, the pool that produces blocks with that weight.

That creates two different, equally valid answers to "how much stake does this
key have", and picking the wrong one quietly excludes everybody who delegates.

## The chain's default answer is by signer

`getstakerinfo` returns EFFECTIVE weight keyed by signer, because that is what
block production needs to know. Under it:

- a controller that has delegated **does not appear at all**;
- its pool appears carrying weight that is not the pool's own.

A launchpad reading that view would tell a delegator they have no stake, while
handing their pool operator a tier bought with other people's money.

## Levo asks by controller

Levo is asking a different question -- whose money is this? -- so it reads raw
weight keyed by controller, via `getstakerinfo verbose=true bycontroller=true`.
Under that view:

- a delegated stake counts for the person who owns it, at full weight;
- a pool operator is credited with their own stake and nothing else;
- nobody's weight is counted twice, because each stake has exactly one
  controller.

Delegation changes who signs blocks. It does not change who owns the coins, and
Levo's tiers are about ownership.

## Older nodes

`bycontroller` is a Sequentia RPC addition. Against a node without it, Levo
falls back to the signer-keyed view and says so, in the API (
`counts_delegated_stake: false`) and on the account page. It does not silently
report a delegator as having nothing: that would look like a lost stake rather
than a node that cannot answer the question.

## Proving a delegated stake

Nothing changes. The controller key is still the one that has to sign, because
it is still the key that owns the stake. A wallet that can sign with its
staking key does this in one step -- see `signStakerMessage` in the browser
extension -- and signing in with that key needs no linking at all, since the
login has already proved it.
