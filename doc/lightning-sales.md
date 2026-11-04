# Lightning sales: a sale priced in BTC, paid over Lightning

A Lightning sale sells a Sequentia token for native bitcoin. The buyer pays a
Lightning invoice in BTC, from any Lightning wallet or a Bitcoin Ark balance,
and the payment itself hands over the tokens. Nobody holds the tokens in
between, nobody reads a price from an oracle, and no pegged bitcoin is
involved.

## Why a hash lock

A Sequentia covenant cannot see a Bitcoin output, so it cannot check that a
buyer paid in BTC. It does not need to. Paying a Lightning invoice reveals a
32-byte secret, the preimage of the invoice's payment hash, to the payer at the
moment the payment completes. A script can check that secret. So the tokens are
put where only the secret can deliver them, and the payment is what gives the
buyer the secret.

## The flow

A buyer needs nothing but BTC and an address to receive the tokens at.

1. **Order.** The buyer names an amount and their address. The project's
   receiver, the service beside the project's Lightning node, issues an
   invoice for the lot's BTC price under a secret only it knows.
2. **Pay.** The buyer pays the invoice from any Lightning wallet or a Bitcoin
   Ark balance. The project's node *holds* the payment: it is committed, and
   nobody has it yet.
3. **Lock.** With the payment held, the receiver signs a transaction that
   moves the lot out of the sale into a *lot lock* under the invoice's payment
   hash, naming the buyer's address. It pays that transaction's fee from its
   own coins and funds the lock's fee pot (the *bond lock*), which pays the
   delivery's fee.
4. **Deliver.** Once the lock is in a block, the receiver broadcasts the
   delivery, which needs no signature: the secret sends the lot to the buyer
   and the pot's remainder to the buyer too. Then it settles the payment.

Neither side can cheat. The receiver is paid only by settling, and settling
hands the buyer the secret, which alone delivers the lot: a receiver that
settled without delivering leaves the buyer holding a delivery anyone can
broadcast (`POST …/lightning/<hash>/claim`), with its fee already in the pot. A
payment the receiver cannot back with a lock (the sale emptied, no coins for
the fee) is failed back to the buyer. A payment that goes away after the lock,
failed back or expired, leaves the lot to go back to the sale after the
timeout, and the pot to the receiver. An order nobody pays locks nothing.

## A sale that vests

A sale with `vest_locktime` sells tokens that cannot move before that date. Its
lots are not locked under the payment hash; they are paid into the buyer's
wallet as a holding nobody can move before the vesting date, an Arca vesting
claim (Arca's `vest-1` leaf: its every spend checks the date, so neither the
project, nor the operator, nor both together can move it earlier). The buyer
signs in to Levo with their wallet, and the lot is paid to that key; the
wallet shows it in its balance at once, as "vests on <date>", and takes it as
an ordinary holding from the date on.

1. **Order** and **Pay** as above; no address is asked for.
2. **Pay the lot.** With the payment held, the receiver pays the lot from the
   project's Arca wallet as a vesting claim to the buyer's key. The same
   distribution spends every coin of the token the wallet holds, with the rest
   as a vesting claim to the project's own key, because the operator takes no
   change below its smallest leaf; the receiver takes those claims up once they
   vest. When the wallet holds too few tokens for that, the receiver first moves
   the lot, a fiftieth more (the operator's fee is at most a hundredth) and the
   operator's smallest leaf of the token out of the sale to the wallet's own
   address with RESERVE, with a coin for the board's fee, and boards them. When the wallet holds several coins it first pays itself one coin of the exact amount, which leaves a margin for its own fees out of the asset; the project's own claim gives that margin up.
3. **Settle.** Once the operator has released the claim, it is the buyer's,
   whatever anyone does next: only the buyer's key can take it up after the
   vesting date. Then the receiver settles the payment.

The receiver is paid only by settling, and it settles only once the claim is
the buyer's. A payment it cannot back so (the sale emptied, the tokens not in
the wallet within three hours) is failed back. A payment that goes away before
the lot is paid leaves whatever was moved in the project's wallet. What a
buyer's wallet has not taken up by the deadline (`LEVO_RECEIVER_VEST_DAYS`
after the vesting date, 30 by default) goes back to the project, as every
Arca claim's lapse does. Within three hours of the vesting date, a lot is
delivered as in any sale, to the buyer's address.

For now a sale can vest at most 27 days ahead: Levo refuses to list a later
date, and the receiver refuses to pay a lot that vests later. Wallets released
before that was fixed (Ambra mobile 0.17.17 to 0.17.21, Ambra browser 0.5.82
to 0.5.86) leave an allocation stuck when they take it up more than 28 days
after the round that paid it (ConcatenaLabs/arca#577); the limit goes once
those are gone.

A sale that vests has no SELL leaf, so it sells only this way: a direct
purchase would hand over tokens that move at once. The receiver's Arca wallet
must be joined to an operator that serves the sale's token and builds batches
without a sweep, the only batches that hold vesting claims.

## Listing one

A Lightning sale is an ordinary listing whose terms add `lightning`:

| Field | Meaning |
|---|---|
| `receiver_xonly` | The receiver's key, in the address. `bin/levo keygen` makes a pair. |
| `receiver_url` | Where the receiver answers. Levo asks it for invoices and signatures. |
| `btc_num`, `btc_den` | The BTC price: satoshis per token atom, rounded up per lot. |
| `bond_asset`, `bond_atoms` | The fee pot each lock carries, in an asset nodes take fees in. The receiver funds it; what the delivery's fee leaves goes to the buyer, so keep it just above the allowance. |
| `fee_allowance` | The most the pot may pay as the delivery's fee. |
| `timeout_seconds` | How long a locked lot waits for its delivery before it may go back to the sale (default six hours). |
| `vest_locktime` | Optional, a unix time at most 27 days ahead: the tokens cannot move before it (see [A sale that vests](#a-sale-that-vests)). |

The rest of the terms are the ordinary sale's, so the same address also sells
directly in the asset the sale settles in, through the SELL leaf, unless the
sale vests.

## Running the receiver

`levod/receiver.py` is the project's process, on the project's machine, beside
its Lightning node, which runs the holdinvoice plugin
(`contrib/holdinvoice-seq` in the SeqLN repository). It issues an invoice per
order and registers it to be held, locks a lot only once its payment is held,
delivers it, settles, and re-rests any lock whose payment went away. Its own
coins, which pay the locks' fees and fund the pots, rest at its key's taproot
output, which `GET /health` names; keep a few there in the bond asset.
`contrib/levo-receiver.service` runs it; `contrib/levo-receiver.env.example`
lists its settings. Its invoices expire long before a lock does, and it refuses
a sale whose timeout leaves less than half an hour between the two.

## The scripts

All three are taproot outputs over the NUMS internal key, so none has a key
path. `levod/lnsale.py` builds them; `levod/tests/test_lnsale.py` freezes their
addresses.

| Output | Leaves |
|---|---|
| Sale | RESERVE: the receiver's signature; output 2k carries the lot (at least the minimum, in the token, explicit) and output 2k+1 re-rests any remainder (at least the minimum) at this very address. SELL, unless the sale vests: the ordinary sale's permissionless leaf, byte for byte. RECLAIM: the project's key from the close. |
| Lot lock | CLAIM: the 32-byte secret of the payment hash, and the output at the input's own index pays all of this input's value, in the token, to the destination. No signature. RE-REST: after the timeout, the output at the input's own index pays all of it back to the sale address. No signature. |
| Bond lock | REFUND: the secret, and the output at the input's own index returns the bond, in its asset, to the buyer's refund address, less at most a fee allowance the lock fixes. No signature. FORFEIT: after the timeout, the receiver's signature. |

Timeouts are relative and time-based (BIP68 units of 512 seconds), and vesting
is an absolute time, checked by the Arca claim's own scripts. No lock counts blocks.

## What the chain enforces, and what it does not

Enforced by consensus, each proven on a regtest chain by mining the refused
spend with `generateblock` (`levod/tests/test_lnsale_node.py`):

- Nobody takes a lot without the secret, and the secret can only send all of
  it to the buyer's destination.
- Before the timeout a lot cannot go back to the sale, and the project cannot
  take the bond. With the secret the bond can only go back to the buyer, less
  at most the fee allowance.
- After the timeout a lot can only go back to the sale, all of it.
- A reservation needs the receiver's signature and keeps the remainder at the
  sale address.
- A sale that vests has no SELL leaf, so nothing but the receiver's key takes
  tokens out of it.
- A vesting lot cannot move before its date: Arca's `vest-1` scripts check it
  in every path (proven in Arca's own regtest suite).

Not enforced, and why:

- **The invoice amount is the project's.** The chain cannot see a Bitcoin
  price. The buyer's wallet compares the invoice with the published BTC price
  before paying.
- **Delivery must happen before the timeout.** The receiver delivers as soon as
  the lock is in a block. If it settles and does not, the buyer, holding the
  secret, must broadcast the delivery before the timeout; after it the lot and
  the delivery race.
- **The receiver's key moves lots out of the sale.** The project knows the
  secret of every invoice it issues, so it can always take its own lots, the
  same way it can always buy its own sale out at the published price. The key
  is the project's, like the treasury; keep it like one.
