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

## Two forms of the same transaction

A node signs raw hex: `signrawtransactionwithwallet` looks every input up for
itself and leaves the covenant input's witness alone. A browser wallet gets no
such look-up, so Levo also returns the transaction as a PSET, with the output
each input spends attached and the covenant's witness already final. The wallet
signs the buyer's inputs, finalises, and broadcasts; it cannot alter the
covenant's half, and the transaction id is the same either way.

## The trap in a full buy

On a full buy there is no remainder, and the covenant decides whether one exists
by asking whether output `2k+1` carries the token asset. If the buyer's own
tokens sat there, the leaf would read them as an unsold remainder and demand
they be sitting at the sale address instead.

So on a full buy, output 1 must be an explicit output that is not the sale
token: payment-asset change, or a fee in another asset. A fee paid in the sale
token cannot take that slot either. `build_buy` enforces all of it, and refuses
a full buy that has nothing to put there rather than producing a transaction
the chain will reject.

## What has to be explicit

Everything the leaf looks at. A confidential output commits to its value
instead of stating it, and the sell leaf refuses any value it cannot read.

That has three consequences worth stating plainly, because each costs money:

- **A buyer's funding inputs must be unblinded.** A blinded input cannot balance
  against explicit outputs without blinding factors that levod does not hold.
  The transaction builds, signs, and is rejected at relay with
  `bad-txns-in-ne-out`, which explains nothing. levod looks each input up on the
  node and refuses a confidential one with a reason.
- **Output 1 must be explicit.** The leaf inspects that output's asset, and a
  confidential asset aborts the script. Confidential outputs at index 2 or
  later are ignored. Every output Levo builds is explicit.
- **A project must lock into an unblinded output.** Tokens locked into a
  confidential output can never be sold: the sell leaf cannot read them. The
  reclaim leaf reads nothing, so the project could still take them back after
  the close with the blinding factors it holds, but the sale would be dead.
  `confirm_lock` refuses a confidential lock outright.

## What the leaf does not check

The sell leaf reads the value of the input it spends, not its asset. Anything
that lands at a sale address can therefore be spent through the sell leaf by
anyone who pays the sale's price for it: a payment sent to the sale address by
mistake is buyable at the token's price. The lock instructions say to send
nothing but the token, and the watcher reports any other asset resting at the
address as a stray, so the project can sweep it after the close. A leaf that
pins the input's asset would move every sale address, which is a vectors
migration rather than a patch.

The sell leaf also carries no locktime. The close opens the reclaim path; it
does not shut the sell path, so until the project reclaims, a buyer who builds
the transaction can still fill the sale. Levo stops planning purchases at the
close and tells the project to reclaim promptly.

## Signatures

The sell leaf carries none. It is introspection-driven, so the witness is just
the leaf script and its control block, and the sale settles with nobody online:
not the project, not Levo, not a market maker.

The reclaim leaf carries one, from the project's own key, over the Elements
taproot sighash. That sighash commits the genesis block hash twice, so a
signature cannot be replayed onto another chain, and it commits each spent
output's asset *and* value commitments rather than a bare amount. levod returns
the sighash and never signs it; the project signs on its own machine, and
`bin/levo reclaim` does so with the key it is handed.

## Byte order

Asset ids are displayed in the reverse of their wire order, like txids. Levo
carries and shows the display form, because that is what a user can compare
against an explorer, and reverses at exactly two boundaries: the constants
compiled into a covenant leaf, and the asset commitment in a transaction output.

Getting this wrong does not fail loudly. It builds a sale denominated in an
asset nobody holds, which no buyer can ever fill and whose tokens are locked
behind it. `levod/tests/test_tx.py` pins the byte order against a transaction
a live node decoded.

## Locktimes

A close is an absolute locktime: a block height below 500,000,000, a unix time
at or above it, and never larger than the transaction's 32-bit locktime field,
because a reclaim leaf that can never be satisfied would leave unsold tokens
locked forever. A reclaim's own locktime must be of the same kind as the close,
since `CHECKLOCKTIMEVERIFY` compares heights with heights and times with times.
For a time close, the chain judges against median time past, which trails the
wall clock by a few blocks; levod builds a reclaim only once the chain's clock
has passed the close.

## Who knows what a sale is

The chain. A watcher reconciles every sale against the UTXO set and the
mempool, because the sell leaf is permissionless and purchases happen that Levo
never planned. A partial buy re-rests the remainder at the identical address,
so the watcher finds a smaller output at a new outpoint and carries on.

It looks in four places, cheapest and most certain first: the outpoint it
already knows, which `gettxout` answers with the mempool included; any outpoint
a purchase recorded through Levo said the remainder would rest at, so a buy
moves the sale the moment it is broadcast; the confirmed UTXO set; and the
mempool itself, for a transaction spending the known outpoint. It ends a sale
only after two silent polls with a new block between them, because a remainder
in the mempool is invisible to the confirmed-set scan until a block carries it.

Three states look identical from the address alone and mean different things.
A sold-out sale, a sale whose funding was undone by a Bitcoin-driven reorg, and
a sale the project reclaimed after the close all leave nothing at the address.
They are told apart by evidence the watcher keeps as it goes: the block each
resting output was mined into, and the transaction id of any reclaim Levo
built. If the chain no longer has the funding's block at that height, the
funding went with it, and the sale is a `GHOST` -- not funded, not investable,
and recoverable by locking again. If the block is intact and the sale had not
closed, only buys could have emptied it: `SOLD_OUT`, which is final. If it had
closed, the sale is `CLOSED` and empty, and becomes `RECLAIMED` when the
reclaim's own output is seen. Absence of the transaction is NOT evidence: a
node without `-txindex` cannot find a fully spent transaction either, and
treating that as a reorg would report a sold-out sale as one that never
existed.
