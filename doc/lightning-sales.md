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

1. **Reserve.** The buyer asks for a lot. The project's receiver, the service
   that runs the project's Lightning node, issues an invoice for the lot's BTC
   price and signs a transaction that moves the lot out of the sale into a
   *lot lock* under the invoice's payment hash. The lock names the buyer's
   destination. The buyer adds a small *bond* under the same hash, against
   reserving without paying, and pays the transaction's fee. The receiver
   signs before the buyer does: its signature covers this one transaction and
   nothing else, so a reservation the buyer never broadcasts costs nobody
   anything.
2. **Pay.** Once the lock is confirmed, the buyer's wallet checks it (the
   hash, the destination, the amount, the time left) and pays the invoice.
3. **Claim.** The secret delivers the lot to the destination the lock names,
   and returns the bond, less the claim's fee, to the buyer's refund address.
   Neither needs a signature, so whoever holds the secret can broadcast the
   claim: the buyer's wallet, or Levo once the receiver reports the invoice
   paid. It can only pay the buyer.
4. **Unpaid.** After the lock's timeout the lot goes back to the sale address,
   and the project keeps the bond.

## Listing one

A Lightning sale is an ordinary listing whose terms add `lightning`:

| Field | Meaning |
|---|---|
| `receiver_xonly` | The receiver's key, in the address. `bin/levo keygen` makes a pair. |
| `receiver_url` | Where the receiver answers. Levo asks it for invoices and signatures. |
| `btc_num`, `btc_den` | The BTC price: satoshis per token atom, rounded up per lot. |
| `bond_asset`, `bond_atoms` | The bond a reservation holds, in an asset nodes take fees in. |
| `fee_allowance` | The most the bond may pay as the claim's fee on its way back. |
| `timeout_seconds` | How long a lot waits for payment (default six hours). |
| `vest_locktime` | Optional: tokens reach the buyer's key only from this time. |

The rest of the terms are the ordinary sale's, so the same address also sells
directly in the asset the sale settles in, through the SELL leaf.

## Running the receiver

`levod/receiver.py` is the project's process, on the project's machine, beside
its Lightning node. It issues an invoice per reservation, signs a reservation
only for an invoice it issued and a transaction it rebuilt itself, and follows
every lock it signed: paid, it broadcasts the delivery; unpaid past the
timeout, it re-rests the lot and keeps the bond. `contrib/levo-receiver.service`
runs it; `contrib/levo-receiver.env.example` lists its settings. Its invoices
expire long before the lock does, and it refuses a sale whose timeout leaves
less than half an hour between the two.

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
- **The buyer must claim before the timeout.** A receiver can hold a Lightning
  payment until its expiry, so a wallet pays only while the time left on the
  lock is longer than the longest the payment can be held, with a margin to
  claim in. After the timeout the lot and the claim race.
- **The receiver's key moves lots out of the sale.** The project knows the
  secret of every invoice it issues, so it can always take its own lots, the
  same way it can always buy its own sale out at the published price. The key
  is the project's, like the treasury; keep it like one.
