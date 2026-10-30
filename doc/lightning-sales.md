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
| `vest_locktime` | Optional: tokens reach the buyer's key only from this time. |

The rest of the terms are the ordinary sale's, so the same address also sells
directly in the asset the sale settles in, through the SELL leaf.

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

All four are taproot outputs over the NUMS internal key, so none has a key
path. `levod/lnsale.py` builds them; `levod/tests/test_lnsale.py` freezes their
addresses.

| Output | Leaves |
|---|---|
| Sale | RESERVE: the receiver's signature; output 2k carries the lot (at least the minimum, in the token, explicit) and output 2k+1 re-rests any remainder (at least the minimum) at this very address. SELL, when the sale also settles in a Sequentia asset: the ordinary sale's permissionless leaf, byte for byte. RECLAIM: the project's key from the close. |
| Lot lock | CLAIM: the 32-byte secret of the payment hash, and the output at the input's own index pays all of this input's value, in the token, to the destination. No signature. RE-REST: after the timeout, the output at the input's own index pays all of it back to the sale address. No signature. |
| Bond lock | REFUND: the secret, and the output at the input's own index returns the bond, in its asset, to the buyer's refund address, less at most a fee allowance the lock fixes. No signature. FORFEIT: after the timeout, the receiver's signature. |
| Vesting | When the sale vests: the destination a lot lock names, spendable by the buyer's key from the vesting time on. |

Timeouts are relative and time-based (BIP68 units of 512 seconds), and vesting
is an absolute time. No lock counts blocks.

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
- A vested allocation cannot move before its time.

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
