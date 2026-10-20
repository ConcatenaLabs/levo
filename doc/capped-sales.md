# Capped sales

An ordinary Levo sale is permissionless: anyone may buy from it at its price,
and a tier's per-buyer cap is Levo's policy ([tiers-are-policy.md](tiers-are-policy.md)).
A capped sale puts the caps in the sale's address instead. It sells only to the
keys on its allocation list, each up to its own cap, and the chain refuses a
purchase past a cap, or by a key that is not on the list, whoever builds it.
Like an ordinary sale it settles with nobody online but the buyer: no Levo
signature, no project signature, no server in the path.

## What the project lists

The ordinary terms, plus `allocations`: a list of `{key, cap_atoms}`, each an
x-only public key in hex and the most that key may buy, in atoms of the token.
`levo keygen` prints a key pair a buyer can use. A key appears once; every cap
is at least the minimum purchase; a list holds up to 65,536 buyers. The list is
the project's decision, made before the sale is funded, and Levo's tiers are a
natural way to draw it up: what the chain guarantees is that the list, once it
is in the address, is the one that binds.

## The covenant

A capped sale is the `sequentia/capped-sale` covenant of
[sequentia-contracts](https://github.com/ConcatenaLabs/sequentia-contracts)
(`docs/capped-sale.md` there is its specification), a Simplicity program.
Levo carries it in `levod/capped_sale/`, pinned to a commit and checked on
every start (`PIN.json`), with the repository's address code, which needs no
compiler, in `levod/sequentia_address.py`.

```
P2TR(NUMS, {{sale, H_TapData(terms ‖ ALLOCATIONS)}, reclaim})
```

`ALLOCATIONS`, the coin's state, is the root of a Merkle tree of (key, cap,
bought) per buyer, every `bought` starting at zero. A purchase shows the
buyer's leaf and its proof, is signed by that key over the whole transaction,
pays the treasury at output 0, and re-rests the rest at output 1 in the state
where only that buyer's `bought` has grown. The program checks all of it,
including that the coin holds the sale's token, so nothing else sent to a sale
address is ever sold. The reclaim leaf is the ordinary sale's: the reclaim key
after the close.

Because the state is in the address, the address moves with every purchase.
The sale page shows the current one, the state root and every key's running
total; levod follows the coin from spend to spend, through purchases it built
and purchases it never saw, by finding the one key whose next state derives the
address the rest landed at.

## Buying

A purchase is signed by the buyer's key on the list, so the buyer's wallet
builds it.

### From a browser wallet

A browser wallet whose contract key is on the list buys from the sale page,
with no Levo account: the buyer is the key, and the cap is the chain's. The
page asks the wallet for its contract key (`getContractKey`), shows that key's
cap and what it has bought, and asks levod for the purchase
(`POST /api/projects/<slug>/wallet-purchase`). levod answers with what the
wallet's contract engine needs: the `sequentia/capped-sale` template, the
sale's instance in its current state, the coin, the buyer's leaf and its proof,
and the outputs the covenant requires. The wallet (`signContractSpend`, the
Sequentia Wallet Kit's engine) refuses a purchase past the cap before anything
else, recomputes the sale's address, adds its own coins for the payment and
the fee, runs the program against the final transaction, shows the purchase
under its five-point approval, and signs with its contract key only, a
signature over this purchase and no other. The fee is in the asset the sale is
paid in where the network accepts it, and otherwise in the accepted asset
whose largest coin in the wallet covers the most fees. Give the project the
key the wallet's `getContractKey` returns to be listed.

### From your own node


```sh
bin/levo buy <sale> --tokens 100 --buyer-key <32-byte hex secret> --seqc <path to seqc>
```

levod builds the transaction and the signature hash, and Levo signs nothing.
`levo buy` checks that the sale's state derives the address its coin is at on
your own node, recomputes the signature hash itself with `seqc` (the contracts
toolchain's program runner, from the transaction and the outputs it spends as
your node reads them), refuses if levod's differs, signs, has levod put the
covenant witness around the signature, checks the transaction id is the one it
signed, then signs your own inputs with your wallet and broadcasts.

A purchase costs about 1,100 to 1,250 vB, most of it the program and its proof.

## Running it

levod needs `seqc` to build a capped purchase's witness (`LEVOD_SEQC`). Build it
from the commit `levod/capped_sale/PIN.json` names:

```sh
cargo install --locked --git https://github.com/ConcatenaLabs/sequentia-contracts \
    --rev <commit> sequentia-contracts --bin seqc --root /usr/local
```

levod refuses a `seqc` that compiles the sale to another root than the pinned
one. Without `seqc` levod still lists and watches capped sales; it cannot build
purchases from them.

## What the cap means

The running totals live in the coin, so a cap holds for the coin the project
funds and the coins that descend from it. A second coin sent to a capped sale's
first address would start its own totals from zero: fund a capped sale with
one coin, as Levo's lock does, and send nothing else there.

A capped sale's address cannot be found from its terms alone once a purchase
has moved it, so `levo rescue` refuses one; reclaim it with `levo reclaim`
while a Levo follows it.
