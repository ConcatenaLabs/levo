# How a sale settles

One transaction. That is the whole design, and everything else here is a
consequence of it.

## The transaction

A buy spends the sale covenant and, in the same transaction, pays the project
and hands the buyer their tokens. Its shape is not a convention Levo chose --
the sell leaf reads specific output positions, so the layout IS the contract:

| Output | What | Checked by the covenant |
|---|---|---|
| `2k` | the treasury credit | asset, scriptPubKey, and at least `ceil(n × num / den)` |
| `2k+1` | the unsold remainder | must be the token asset at the IDENTICAL sale address, and at least `min_lot` |
| anywhere else | the buyer's tokens, change, the fee | no |

`k` is the covenant's own input index, taken from `OP_PUSHCURRENTINPUTINDEX`.
Deriving it per input rather than fixing it is what stops two covenants in one
transaction from pointing at a single shared payment: input 0 credits output 0,
input 1 credits output 2, and one payment can never settle two sales.

Levo puts the covenant at input 0, so `k` is 0 in practice.

## The trap in a full buy

On a full buy there is no remainder, and the covenant decides whether one exists
by asking whether output `2k+1` carries the token asset. If the buyer's own
tokens sat there, the leaf would read them as an unsold remainder and demand
they be sitting at the sale address instead.

So on a full buy, output 1 must be anything except the sale token -- change, or
the fee. `build_buy` enforces that, and refuses a full buy that has neither
rather than producing a transaction the chain will reject.

## What has to be explicit

Everything. A confidential output commits to its value instead of stating it,
and the sell leaf refuses any value it cannot read.

That has two consequences worth stating plainly, because both cost money:

- **A buyer's funding inputs must be unblinded.** A blinded input cannot balance
  against explicit outputs without blinding factors that levod does not hold.
  The transaction builds, signs, and is rejected at relay with
  `bad-txns-in-ne-out`, which explains nothing. levod looks each input up on the
  node and refuses a confidential one with a reason.
- **A project must lock into an unblinded output.** Tokens locked into a
  confidential output can never be sold *or* reclaimed: neither leaf can read
  them. `confirm_lock` refuses one outright.

## Signatures

The sell leaf carries none. It is introspection-driven, so the witness is just
the leaf script and its control block, and the sale settles with nobody online:
not the project, not Levo, not a market maker.

The reclaim leaf carries one, from the project's own key, over the Elements
taproot sighash. That sighash commits the genesis block hash twice, so a
signature cannot be replayed onto another chain, and it commits each spent
output's asset *and* value commitments rather than a bare amount.

## Byte order

Asset ids are displayed in the reverse of their wire order, like txids. Levo
carries and shows the display form, because that is what a user can compare
against an explorer, and reverses at exactly two boundaries: the constants
compiled into a covenant leaf, and the asset commitment in a transaction output.

Getting this wrong does not fail loudly. It builds a sale denominated in an
asset nobody holds, which no buyer can ever fill and whose tokens are locked
behind it. It was caught the first time a transaction was handed to a real node
to decode, and `tests/test_tx.py` pins it.

## Who knows what a sale is

The chain. A watcher reconciles every sale against the UTXO set, because the
sell leaf is permissionless and purchases happen that Levo never planned. A
partial buy re-rests the remainder at the identical address, so the watcher
finds a smaller output at a new outpoint and carries on.

Two states look identical and mean opposite things: a sold-out sale and one
whose funding was undone by a Bitcoin-driven reorg both leave nothing at the
address. They are told apart by the block the funding was mined into. If the
chain no longer has that block at that height the funding went with it, and the
sale is a `GHOST` -- not funded, not investable, and recoverable by locking
again. Absence of the transaction is NOT evidence: a node without `-txindex`
cannot find a fully spent transaction either, and treating that as a reorg would
report a sold-out sale as one that never existed.
