# Levo

A launchpad on [Sequentia](https://github.com/ConcatenaLabs/Sequentia).

Levo allocates by commitment. Stake Sequence and you can take a position in a
sale; stake enough and you can run one. Everything in between is settled by a
covenant that holds the project's tokens from the moment they are locked to the
moment they reach a buyer.

*Levo* is Latin for to raise, to lift. Its one drawing is a balance beam: the
more Sequence you stake, the higher your cap in a sale.

## What is real

Everything that protects money is real, and the part that holds the tokens is
enforced by consensus:

- **Sale covenants**, enforced by consensus. A sale is a taproot output with a
  NUMS internal key and two leaves. Tokens can leave it by being sold at the
  published price to the published treasury, or by being reclaimed by the
  project from the close date on. Nothing else. The leaf reads the amount it is
  spending and never which output that is, so this is true of EVERY output at
  the sale address: anything else sent there is buyable at the sale's price
  too, which is why the lock instructions say to send once and send nothing
  else, and why the sale page reports whatever is resting there beside the
  sale.
- **Settlement**, enforced by consensus. A buy spends the covenant, pays the
  treasury and delivers the tokens in one transaction. Levo builds that
  transaction unsigned; only the buyer's wallet can complete it.
  [doc/how-a-sale-settles.md](doc/how-a-sale-settles.md) walks that
  transaction output by output, and what the leaf does and does not check.
- **Capped sales**, enforced by consensus. A sale listed with an allocation
  list sells only to the keys on it, each up to its own cap: the list and every
  buyer's running total are in the sale's address, and the chain refuses a
  purchase past a cap, or by a key not on the list, whoever builds it. See
  [doc/capped-sales.md](doc/capped-sales.md).
- **Buying from leaves**, enforced by consensus and attributed by Levo. A
  holder of leaves of an operator's batches buys from a sale whose treasury
  is a taproot address through the operator's round, which spends the sale
  once for many buyers: the covenant judges that spend as it judges any
  purchase, at whatever input the round puts it, and each buyer gets a leaf of
  its own share. Levo attributes each share to its buyer from the operator's
  status of the take and the round it reads on its own node, and counts it
  once the round is in a block and for as long as the block stays in the
  chain. See [doc/how-a-sale-settles.md](doc/how-a-sale-settles.md) and
  [doc/api.md](doc/api.md).
- **Signed-message login**, a signature Levo checks. An account is a public key.
  Levo issues a challenge, a wallet signs it, and Levo recovers the key from the
  signature. The signed text has to be the issued challenge, word for word.
- **Stake, read from the chain.** The weight behind each key comes from the
  node, counted only for staker keys an account has proven it controls, and
  read by CONTROLLER rather than by signer, so a stake delegated to a pool still
  counts for the person who owns it. See [doc/delegated-stake.md](doc/delegated-stake.md).
  Levo names no signer to delegate to: it says how much stake each tier needs,
  own or delegated, and links to a wallet's staking screen and to the public
  staking pools page, which shows whether each pool is producing. The holder
  chooses there.
  The tier boundaries that weight maps to are Levo's configuration, not a chain
  rule.
- **The chain decides.** A watcher reconciles every sale against the UTXO set
  and the mempool, so a purchase made without Levo still moves the sale, and a
  lock undone by a Bitcoin-driven reorg stops being investable. It decides on
  positive evidence only: a sale becomes a ghost when the chain says its
  funding is gone -- the block it was mined in is no longer at that height, or
  it was never mined and is in no block and no mempool -- and never merely
  because nothing was found. A sale whose funding cannot be placed at all is
  left as it was and reported as unverified, in `/api/health` and on its own
  page.

Two things are deliberately **not** consensus, and the code says so wherever
they appear. **In an ordinary sale, per-buyer tier caps are Levo's allocation
policy.** The sell leaf has a minimum purchase but no maximum, and it is
permissionless by design; see [doc/tiers-are-policy.md](doc/tiers-are-policy.md).
A capped sale is the exception, by construction. **The close date opens the
reclaim path; it does not shut the sell path.** Levo stops planning purchases at
the close, but a buyer who builds the transaction can still fill a sale until
the project reclaims what is left.

One more thing the covenant does not rule out, stated so nobody has to discover
it: the treasury is the project's own key, so a project can always buy its own
sale out at the published price, on chain and in the open, for the cost of the
fee. What it cannot do is take the tokens by any other route, or change the
terms a buyer sees.

## Proven on chain

Both covenant paths are exercised on the Sequentia testnet: a buy that pays the
treasury and re-rests the remainder at the identical address
([`43636934…1a7a`](https://sequentiatestnet.com/explorer/tx/43636934b5289a45fa3860489112ebf1659a1c55395cc3ea62da167513981a7a)),
and a reclaim through the reclaim leaf after a close set by block height
([`2c980cd0…08ee`](https://sequentiatestnet.com/explorer/tx/2c980cd0ed69d5a0052005111840f16ff9d0247ca643bcf067b4b516436f08ee)),
both from one sale
([reclaim-drill](https://sequentiatestnet.com/levo/p/reclaim-drill)) that was
listed, locked, bought from and reclaimed entirely with `bin/levo`.
The platform runs sales end to end at
[sequentiatestnet.com/levo](https://sequentiatestnet.com/levo/). You do not
have to take that on trust: every sale publishes the terms its address was
derived from, so rebuild the address and compare it to the funded output.
`bin/levo verify <sale>` does exactly that against your own node.

## Paying

Levo shows every amount in **BTC**, and in US dollars at the flip of a switch
the page remembers for you. Both come from the fee exchange-rate table of the
node Levo reads (`GET /api/rates`).

**A sale settles in the asset its project chooses**: any asset the node's rate
table prices, USDX or another stablecoin among them, and never pegged bitcoin.
That asset settles inside the covenant: buyer and project exchange in one
transaction, enforced by consensus.

**BTC** is native Bitcoin on the parent chain, not a token on Sequentia and not
a pegged claim on one, so a Sequentia covenant cannot read a Bitcoin output. A
BTC purchase is two separate steps: swap BTC for the payment asset over
Lightning in your own wallet, then fill the covenant with it. Each step is
atomic on its own; between them you hold the payment asset, and you can stop
there. Levo takes custody of neither step. The quote comes from the fee
exchange-rate table of the node Levo reads, the same table that node's mempool
uses to price a fee paid in any asset. That table is the node operator's
policy, not a consensus price, so treat a BTC quote as Levo's quote and check
it against your own wallet before swapping.

**Native BTC over Lightning.** A sale can also be priced in BTC and paid with
a Lightning invoice, from any Lightning wallet or a Bitcoin Ark balance; the
buyer needs nothing else. The project's node holds the payment while the lot is
locked under the invoice's payment hash, and the secret that settles the
payment is the one that delivers the lot. Nobody holds the tokens or the BTC
in between and no pegged bitcoin is involved. A project runs a small
receiver beside its Lightning node for this
([doc/lightning-sales.md](doc/lightning-sales.md)).

## Who does what

**A buyer** needs a Levo account (a key that can sign a message: the browser
extension in one click, or by pasting a signature the web wallet's Sign tab,
Ambra's Sign screen, or any wallet that signs a message the way
`sequentia-cli signmessage` does), staked Sequence under a key they can prove
they control, and the sale's asset, unblinded. A browser wallet signs and broadcasts the
purchase in place: Levo hands it a PSET whose covenant input already carries
its witness, and the wallet signs only the buyer's own inputs.
That needs a wallet that fills in its own key origins before signing, which it
announces as the `pset-site-built` capability; where the wallet cannot, Levo
offers the node path instead and says so. A node signs the same purchase with
`signrawtransactionwithwallet`, or lets `bin/levo buy` do the whole thing.
A capped sale's buyer needs no account: it is a key on the sale's list, and a
browser wallet whose contract key is listed builds and signs the purchase
itself ([doc/capped-sales.md](doc/capped-sales.md)).

**A project** needs a tier that may list, an issued asset (registered if it
wants wallets to show its name), the whole allocation in a wallet it can send
from, an address for the treasury, and a reclaim key it can sign with outside a
browser wallet, because reclaiming means signing a raw sighash; `bin/levo
keygen` makes one. A second round of the
same token needs a fresh reclaim key: the amount for sale is not part of the
sale address, so two rounds at the same price and close would otherwise derive
one covenant, and Levo refuses that rather than let two sales share an
address. It should keep the
sale's terms beside that key: `bin/levo terms <sale> --out sale.json`, or the
same file from the sale's own page. The address is made of those values, and so
is the leaf the key spends through, so with the file, the key and any node,
`bin/levo rescue --terms sale.json` sweeps what is left after the close whether
or not Levo still exists, and `--outpoint <txid>:<vout>` sweeps any one output
at that address whatever asset it holds, because the reclaim leaf checks a
locktime and a signature and nothing else. The treasury may be any witness
address the project's wallet hands out, taproot or version-0: the version is
compiled into the leaf beside the program, so a wallet without taproot addresses
can still run a sale. The project locks
its tokens by sending them to the sale address, and Levo finds the lock on
chain, whether the project confirms the send or the watcher finds it on its
own; after the close, `bin/levo reclaim` sweeps what did not sell.

**What Levo keeps.** An account is a public key. Levo keeps that key, the
listings it makes and the purchases recorded for it -- the allocation ledger
-- in the state file on the operator's server, with copies on the same
machine; a visitor's session is a token in their browser's local storage,
good for twelve hours, and there are no cookies. The request log keeps the
address a request came from with the path it asked for and the answer's
status, and the address counts against the rate limits. The pages load
nothing from anywhere else: no analytics, no third-party scripts, and the
fonts are served by the app itself. How it works says the same to visitors.


## Layout

| Path | What |
|---|---|
| `levod/` | The backend. Pure Python, standard library only. Serves the API and the built app from one origin. |
| `levod/server.py` | The service: the API under `/api/` and the built app, one origin. Every page is served with its own title and description in its head, a sale's page with the sale's name and one-liner, so a link previews as what it points at wherever it is shared. `levod/demo.py` is the same server over a stub node, with three seeded sales: one open, one not yet funded, one reclaimed. |
| `levod/covenant.py` | The sale covenant, checked byte for byte against `levod/vectors.json` on every import. |
| `levod/capped.py`, `levod/capped_sale/`, `levod/sequentia_address.py` | The capped sale: the `sequentia/capped-sale` covenant of sequentia-contracts, vendored and pinned (`capped_sale/PIN.json`, checked on every import), the compiler-free address code it is derived with, and the purchase witness built through `seqc`. |
| `levod/receiver.py` | A project's Lightning receiver: invoices it holds, the lock it signs and funds once a payment is held, the delivery, and the re-rest of a lock whose payment went away. The project runs it; levod holds no key. |
| `levod/lnsale.py` | The Lightning sale's scripts: the sale address with its RESERVE leaf, the lot lock and the bond lock a reservation creates, and the vesting destination ([doc/lightning-sales.md](doc/lightning-sales.md)). |
| `levod/sale.py`, `levod/market.py` | The sale lifecycle (lock, sell, reclaim) and the marketplace rules: listing, the settlement plan, the per-buyer caps, the ledger. |
| `levod/tiers.py` | The tier policy, and the `LEVOD_TIERS` table it reads. |
| `levod/rails.py` | The payment rails and their quotes, and the node's rates every amount is shown in BTC and dollars with. |
| `levod/auth.py` | Login: a challenge, a signature, and the key recovered from it. |
| `levod/store.py` | The state file: listings and the allocation ledger, written atomically. |
| `levod/tx.py`, `levod/pset.py` | The transaction that settles a buy (as raw hex and as a PSET), and the one that reclaims what did not sell. |
| `levod/watcher.py` | Reconciles sales against the UTXO set and the mempool; the chain is the source of truth. |
| `levod/secp256k1.py`, `levod/script.py`, `levod/address.py` | Curve, script and address primitives. Levo carries its own so it needs no node source checkout. |
| `levod/rpc.py`, `levod/units.py` | The node connection, and atoms to and from the decimal strings everything else speaks. |
| `levod/registry.py` | What an asset registry says a token is, if one is configured. Advisory: a listing that contradicts a registered contract is refused, an unregistered asset lists as it is, and a registry that cannot be reached blocks nothing. |
| `tools/gen_vectors.py` | Regenerates `levod/vectors.json`. Running it is a migration, not a refresh; see [CONTRIBUTING.md](CONTRIBUTING.md). |
| `tools/smoke.py` | Meets a running Levo from the outside after a deploy: health, every route in a real browser, the sitemap, a sale's social card, the headers, and a paste-signature sign-in when given a key. |
| `bin/levo` | A CLI that runs the whole flow against your own node. |
| `levod/tests/cdp.py` | A small Chrome DevTools client, so the browser suite can press the buttons rather than only look at the pixels. |
| `web/` | The single-page app: Vite and React, plain CSS, fonts served from the app itself. |
| `contrib/` | The deploy script, the systemd units (sandboxed), an environment file to fill in, a backup timer for the state file, a five-minute health check, and an alert hook that tells a person when a unit fails, with the restore drill in its README. |
| `doc/` | The HTTP API ([`api.md`](doc/api.md)), how a sale settles, capped sales, why tier caps are policy, and why delegated stake counts for its owner. |

## Running it

```sh
npm --prefix web install && npm --prefix web run build   # build the app once
python3 levod/demo.py                                    # then open http://127.0.0.1:8099
```

levod runs on **Python 3.8 or later** and needs nothing outside the standard
library: every file parses under the 3.8 grammar, and the one stdlib piece
with a floor, `ThreadingHTTPServer`, arrived in 3.7. The app builds with
**Node 20.19 or later, or 22.12 or later** (Vite's engine range). On an older Node the native bundler binding is skipped as an unmet
engine and the build fails on a missing module rather than on the version,
which is a confusing way to find out.

Serving from a sub-path (`/levo/` behind a reverse proxy that strips the
prefix) needs `LEVO_BASE=/levo/ npm run build`; the app reads its own base at
runtime, so nothing else changes.

`demo.py` replaces the node with a stub and seeds three
sales, so the whole
platform can be clicked through without a chain. Covenant addresses, signature
recovery and tier arithmetic are the shipped code; only the node is faked.

Against a real node:

```sh
export LEVOD_RPC_URL=http://127.0.0.1:18776            # the testnet's RPC port
export LEVOD_RPC_COOKIE=~/.elements/testnet3/.cookie    # or LEVOD_RPC_USER/PASSWORD
export LEVOD_SECRET=$(head -c32 /dev/urandom | xxd -p -c64)
python3 levod/server.py
```

## The command line

`bin/levo` drives a sale against your own node. It talks to a levod over HTTP
and to your node through `sequentia-cli`, and never sends a key anywhere:
signing happens in your wallet.

```sh
export LEVO_URL=https://sequentiatestnet.com/levo
export LEVO_SIGN_WIF=<the WIF of your staking key>   # so your tier is recognised at once
bin/levo sales
bin/levo verify helios-grid                          # rebuild the address, compare with the chain
bin/levo buy helios-grid --tokens 40                 # picks unblinded inputs, builds, signs, broadcasts
```

| Variable | Meaning |
|---|---|
| `LEVO_URL` | The levod to talk to. Default `http://127.0.0.1:8099`. |
| `LEVO_SESSION` | Where the signed-in session is kept. Default `~/.levo-session`. Delete it to sign out; a session is good for twelve hours, after which the next command asks you to sign in again. |
| `SEQUENTIA_CLI` | The node's CLI binary. Default `sequentia-cli` on `PATH`. |
| `SEQUENTIA_DATADIR` | Passed as `-datadir`, for a node in a non-default place. |
| `SEQUENTIA_WALLET` | Passed as `-rpcwallet`, when several wallets are loaded. |
| `LEVO_SIGN_WIF` | Sign in with this key instead of a wallet address. A staker who signs in with the staking key's WIF is recognised at once; there is nothing to link. Without it, the first sign-in creates a legacy address labelled `levo-login` and every later sign-in reuses it, so the account stays the same key. The node's message signing works with legacy addresses only. |
| `LEVO_RECLAIM_KEY` | The 32-byte hex reclaim secret, for `levo reclaim` (or pass `--reclaim-key`). |
| `LEVO_BUYER_KEY` | A capped sale's buyer: the 32-byte hex secret of a key on its allocation list, for `levo buy` (or pass `--buyer-key`). |
| `LEVO_SEQC` | The contracts toolchain's `seqc`, with which `levo buy` computes a capped purchase's signature hash on this machine (or pass `--seqc`). |
| `LEVO_TRACE` | Set to `1` to print the full traceback when a command fails, instead of the one-line sentence. |

`levo --help` lists every command: `sales`, `show`, `verify`, `whoami`,
`positions`, `buyers`, `link`, `keygen`, `create`, `lock`, `buy`, `order`, `order-status`, `record`, `reclaim`, `terms`,
`rescue`, `withdraw`, and `flag` for an operator. `levo --version` names the
commit the checkout is at, which is the one fact a report about a levo needs,
since Levo has no version number. `record` exists for a
purchase built somewhere other than Levo: one Levo built is recorded by Levo
itself as soon as its node sees the treasury credit, in the mempool or in a
block, since it knows the transaction's id before anything is signed. Levo
reads its own node, which is not the node a purchase was broadcast to, so for a
few seconds after a broadcast it has not heard of the transaction, and a record
that did not go through then can be made whenever. Fees are never defaulted to
the policy asset: `lock`, `buy` and `reclaim` pay them in the sale's payment
asset unless told otherwise, and their size comes from the node's own relay
floor rather than a figure typed in.

Listing from the command line takes a JSON file holding `{"project": {...},
"terms": {...}}`. `bin/levo create --example` prints one to start from, with a
note on what each field means; `bin/levo create listing.json` submits it.

## Tests

```sh
python3 levod/tests/run.py        # everything that needs no node: crypto, covenant, tiers, the request handler
python3 levod/tests/test_e2e.py   # the API end to end, against a stub node
python3 levod/tests/test_node.py  # the whole life of a sale against a real sequentiad; skipped without one
python3 levod/tests/test_render.py # every page, in a real browser; skipped without a chromium
python3 levod/tests/test_cli.py   # bin/levo end to end against a real node; skipped without one
python3 levod/tests/test_capped.py # a capped sale end to end: real node, levod, bin/levo; skipped without a node or seqc
python3 levod/tests/test_lightning_e2e.py # a Lightning sale end to end: sequentiad, bitcoind, two lightningd, the receiver; skipped without them
python3 levod/tests/test_lnsale_node.py # the Lightning sale's scripts on a real chain, every refusal mined into a block; skipped without a node
python3 levod/tests/test_browser.py # a purchase made in a browser, with a wallet; skipped without either
python3 levod/tests/leaves_observer.py --help # buying from leaves: run by arca's shared-fills test, see below
npm --prefix web test             # the formatters, the parsers, and the guards that walk the sources
npm --prefix web run build        # the frontend gate
```

Everything in that list that needs no Sequentia node also runs on every push
and pull request, in `.github/workflows/gate.yml`, every page in a real
browser included. The suites that need a chain say so and skip there. How a
change gets in, and the lines not to cross, are in
[CONTRIBUTING.md](CONTRIBUTING.md).

Schnorr signing is checked against the BIP340 vectors, the taproot tweak
against BIP341's, the transaction serialisation against a txid a live node
computed, the address encoder against an address a live node printed, and the
covenant bytes against frozen vectors. The node test starts `sequentiad` on a
throwaway regtest chain (it looks at `SEQUENTIAD`, `SEQUENTIA_SRC/src`, then
`~/Sequentia/src`) and runs a lock, a PSET purchase, a raw purchase, the
watcher, a sell-out, a reclaim under both kinds of close, a sale whose treasury
is a version-0 address, a funding that never lands, a reorg that unconfirms a
funding and mines it again, a reorg that takes one away for good, a purchase
whose fee is paid in a third asset -- neither the sale's nor bitcoin, which is
what an open fee market means -- prices whose arithmetic does not come out even,
where Levo's ceiling and the leaf's have to agree atom for atom, and every spend
the covenant must refuse. The capped-sale test runs the same kind of chain with
Simplicity active and `LEVOD_SEQC` set: a capped sale listed and locked, two
buyers through `bin/levo` (one up to exactly its cap, then refused one lot
more), a key not on the list refused, a purchase built outside Levo that levod
follows by finding which key made it, and the reclaim after the close.

The render test starts the demo server and paints every route in a headless
Chromium, failing on a console error or a page that painted nothing. It is
there because every other test reads code or talks to the API: a change can
leave the bundle building and the routes answering and still ship a white
screen. It then drives a 300-pixel viewport over the debugging protocol and
fails on the two things a screenshot cannot see: a heading that breaks onto a
line with nothing on it, and a panel still plotted where its labels cannot fit.
Last it runs the audit a screen reader or a colour-blind reader would, on every
page signed out and signed in: a language on the document, one heading at the
top, alt text on every image, a label on every control, a name on every button
and link, and text that clears the contrast its size needs. Four faults are
planted on a page first, to prove the audit bites.

The CLI test starts a node and a levod and drives `bin/levo` as a person would:
sign in, list, lock, verify, price, buy, and reclaim after the close. The CLI
has its own input handling, its own unit conversions and its own wallet calls,
and nothing else here executes a line of it.

The browser test makes a purchase the way a person does. It starts a node, a
levod serving the built app, and a headless Chromium driven over the debugging
protocol, and installs a wallet that is fake only in where it lives: it signs
with the node's keys, spends the node's outputs and broadcasts to the node. It
signs in, prices a purchase, lets the wallet pick the outputs, builds, signs
the PSET, broadcasts, and then checks the transaction that came out -- the
treasury paid what the covenant demands, the remainder re-rested at the sale's
own address, the ledger moved, and the account page showing it -- with no
console error anywhere in the sequence. It needs the app built first.

Buying from leaves needs an operator, so its whole path runs from the other
side: arca's `bark-cli/tests/arca_shared_fills.rs` (in
[ConcatenaLabs/arca](https://github.com/ConcatenaLabs/arca)) starts an anchored
regtest chain, an operator and eleven wallets, and runs a program named by
`ARCA_TEST_OBSERVER` at each step. `levod/tests/leaves_observer.py` is that
program for Levo: it starts a levod serving the built app on the test's chain,
lists the sale the test's wallets take, records each take, drives one buyer
through the sale page in developer mode in a headless Chromium (its stand-in
wallet answers `takeOrder` by running that wallet's own `arca take`), and at
every step checks what levod shows: ten buyers attributed from one spend of
the sale at input 1, every attribution undone when the round's anchor is
orphaned and back when the round is mined again, a buyer whose wallet never
hands its part over dropped and the nine others attributed from the next
round. From an arca checkout:

```sh
ARCA_TEST_OBSERVER=/path/to/levo/levod/tests/leaves_observer.py \
  cargo test -p arca-cli --test arca_shared_fills -- --nocapture
```

with what arca's own tests need (`SEQUENTIAD_EXEC`, `ARCA_TEST_POSTGRES`, the
`arca-signer` binary; arca's `bark-cli/README.md`), and the app built here.

No framework. Those commands are the whole gate, on a laptop and in CI alike.

## What the command line checks before it signs

`levo buy` and `levo reclaim` are handed a transaction by a levod, and neither
takes its word for what it is. Both rebuild the covenant from the terms the
sale published, and then read the transaction with the operator's own node:
the buy checks that input 0 is the outpoint this sale rests on, that output 0
pays the treasury those terms derive exactly what was quoted, that any
remainder goes back to the sale address, that the tokens arrive at the address
that was asked for, and that nothing pays anywhere else; the reclaim rebuilds
the sweep itself and refuses unless the sighash it is being asked to sign is
the one that falls out of its own build. For a capped sale the buy also checks
that the state the sale shows derives the address its coin is at on the node,
and computes the signature hash it signs with its own `seqc`, refusing if
levod's differs. A signature is over a hash, and a hash
says nothing about what it hashes, so the only safe check is the one made
before signing.

## What a buyer's browser checks for itself

The page rebuilds the sale address from the published terms and compares it
with the address it was told, and before anything is offered for signing it
decodes the transaction and reads the outputs out of the bytes: the treasury
credit, the remainder going back to the sale address, and the tokens arriving
at the address the buyer gave. None of that is levod's word for it -- the
description levod sends alongside is shown next to the decoded figures, not
instead of them, and a transaction the page cannot read is one it will not
offer to sign.

## Deployment

The platform is live at [sequentiatestnet.com/levo/](https://sequentiatestnet.com/levo/),
suggesting USDX on the Sequentia testnet as the asset a sale settles in
(`2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de`, eight
decimal places), with `LEVOD_ORIGIN=https://sequentiatestnet.com/levo` and the
explorer at `https://sequentiatestnet.com/explorer`. Those belong to this
deployment: `contrib/levod.env.example` leaves them blank on purpose, because a
fork that inherits them asks its users to sign statements naming somebody
else's site.

The box pulls this repo from GitHub and runs `levod/server.py` under systemd;
`contrib/levod.service` is the unit, and `contrib/levod.env.example` lists every
setting. `LEVOD_SECRET` and the node credentials are supplied through the
environment file the unit reads (`EnvironmentFile=`), never through the repo.
The state file lives outside the checkout (`LEVOD_STATE=/var/lib/levo/levo-state.json`)
and `contrib/levo-backup.sh` with its timer keeps dated copies of it: a funded
sale's leaves are rebuilt from the terms in that file, so it is worth keeping.

`contrib/deploy.sh` puts a commit on the box: it fetches, builds the app under
a Node that meets Vite's engine range, restarts levod, and then asks levod which
bundle it is serving, since a build that fails leaves the previous one in place
with every other check passing. Caddy routes the sub-path to levod's default
port, stripping the prefix:

```
redir /levo /levo/ permanent
handle_path /levo/* {
    encode gzip zstd
    reverse_proxy 127.0.0.1:8099
}
# While levod is down the proxy would answer an empty 502: a blank page with
# nothing said. This answers for Levo's paths and no other.
handle_errors 502 503 {
    @levo path /levo /levo/*
    handle @levo {
        header Content-Type "text/html; charset=utf-8"
        respond <<HTML
<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Levo is restarting</title></head><body><h1>Levo is restarting.</h1><p>The service behind this page is not answering at the moment. Every sale is a covenant on the Sequentia chain, so nothing about any sale changes while this page is down; it is only the page.</p><p>Try again in a minute.</p></body></html>
HTML {err.status_code}
    }
}
```

`encode` belongs in the proxy rather than in levod: the answers worth
compressing are the app's own bundle and the board, and negotiating an encoding
by hand inside a standard-library handler is the kind of code that goes wrong
quietly.

One route covers both the API (`/levo/api/...`) and the app, because levod
serves them from one origin. It also answers `robots.txt`, a `sitemap.xml`
that names every public sale page, and an Atom feed at `feed.xml` with one
entry per public sale, dated by the last thing that happened to it, all built
from the same listings the board shows, so a sale can be found and followed
without running the app; they need `LEVOD_ORIGIN` to write absolute addresses. Uptime checks should watch `/levo/api/health`,
which answers 503 when the node is unreachable, when the watcher has stalled,
or when it runs but every poll is failing -- a watcher that reconciles nothing
leaves sold-out sales showing as open.

An operator named in `LEVOD_OPERATORS` can hide a listing from the board and
put a notice on its page, from the sale's own page or with `bin/levo flag`.
That reaches the page and nothing else: the sale is a covenant on a public
chain, and anyone holding its terms can still buy from it.

[`contrib/README.md`](contrib/README.md) carries the upgrade and the restore, both as commands. The
upgrade reinstalls the systemd units, because a unit change in this repo
reaches the box nowhere else.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LEVOD_HOST` / `LEVOD_PORT` | `127.0.0.1:8099` | Where levod listens. |
| `LEVOD_WEBROOT` | `web/dist` | The built app. |
| `LEVOD_STATE` | `levo-state.json` | Listings, purchases and the allocation ledger. Use an absolute path outside the checkout. A running levod takes the file for itself: a second one on the same file refuses to start rather than overwrite the first's ledger, and the hold is released the moment the process ends, however it ends. |
| `LEVOD_SECRET` | random per start | Session-token key. Set it, or every restart signs everyone out. |
| `LEVOD_RPC_URL` | `http://127.0.0.1:18776` | Sequentia node JSON-RPC. The default is the node's RPC port on chain `test`; the testnet box points it at its own follower node. |
| `LEVOD_RPC_USER` / `LEVOD_RPC_PASSWORD` / `LEVOD_RPC_COOKIE` | — | Node credentials. The cookie path is tilde-expanded, read on every call, and refused at startup if it does not exist. |
| `LEVOD_PAYMENT_ASSET` | USDX on testnet | The asset the listing form suggests and tier caps are written in. A sale may settle in any asset the node's rate table prices; its cap is converted at the node's rates. levod will not start on a value that is not a 64-character asset id. |
| `LEVOD_PAYMENT_DECIMALS` | `8` | How many places that asset divides into. Prices, caps and quotes are all read through it. |
| `LEVOD_BTC_RATE_LABEL` | `SBTC` | Which row of the node's fee exchange-rate table a BTC quote is derived from. |
| `LEVOD_PAYMENT_LABEL` | `USDX` | Its label in the node's rate table and in the interface. |
| `LEVOD_STAKE_LABEL` | `tSEQ` on chain `test`, else `SEQ` | The staking token's ticker in the interface. |
| `LEVOD_HRP` | `tb` | Address prefix: `tb` testnet, `bc` mainnet (Sequentia's unblinded addresses use Bitcoin's own HRPs), `ert` on `elementsregtest`. |
| `LEVOD_TIERS` | supply-share defaults | JSON list of tiers, lowest first, each with `name`, `min_stake` (whole SEQ), `cap` (whole units of `LEVOD_PAYMENT_ASSET`; the most one account may commit per sale, converted at the node's rates for a sale settling in another asset), `may_list`, and a `blurb` that the home page shows beside the tier. Takes effect on restart; caps apply to open sales at once, while what an account has already committed stays committed. |
| `LEVOD_EXPLORER_URL` | — | An esplora-style explorer base (`.../tx/`, `.../address/`, `.../asset/`), for links. |
| `LEVOD_LINKS` | — | JSON of label to URL for the rest of the deployment, shown in the footer. Four labels are also used in the text: `Extension` (where to get the browser extension, which is what signs in here in one click), `Wallet`, `Faucet` and `Staking pools`. |
| `LEVOD_LEAF_OPERATOR` | — | The leaf operator (an Arca server) whose rounds fill sales for buyers paying from leaves. levod reads each take's status there by its id to attribute a share of a round to its buyer, and checks the round itself on its own node. Empty: no take is followed, and a sale page offers no purchase from leaves. See [doc/api.md](doc/api.md), Buying from leaves. |
| `LEVOD_REGISTRY_URL` | — | An asset registry, read as `<url>/<asset id>`. A listing whose ticker or decimals contradict a registered contract is refused; an unregistered asset lists as it is. A registry that cannot be reached blocks nothing. |
| `LEVOD_WATCH_SECONDS` | `60` | How often the watcher reconciles. |
| `LEVOD_SEQC` | `seqc` on `PATH` | The contracts toolchain's `seqc`, which builds a capped sale's purchase witness ([doc/capped-sales.md](doc/capped-sales.md)). Without it capped sales are listed and watched, and their purchases cannot be built. |
| `LEVOD_REQUEST_DEADLINE` | `20` | Seconds one request may take from its first byte to its last, head and body together. A socket timeout re-arms on every read and bounds nothing on its own; this is the wall clock. |
| `LEVOD_PER_PEER` | `8` | Handler slots one address may hold at once. Addresses in `LEVOD_TRUSTED_PROXIES` are exempt, because every request behind a proxy arrives from the same address. Set it 0 to disable. |
| `LEVOD_CLIENT_TIMEOUT` | `10` | Seconds one connection may take to send its request. A client that opens a socket and stops holds a handler slot for exactly this long. Raise it only where levod is exposed directly to slow links rather than through a reverse proxy. |
| `LEVOD_API_ONLY` | off | Serve the API and no app. Set it when nothing is built into `web/dist`, so the health endpoint does not report a site that answers 404 for every page as healthy. |
| `LEVOD_DUST_RELAY` | `100` | The node's dust rate, in reference units per kvB. A node will not relay an output worth less than this rate charges for it, and it does not report the figure, so it is set here. Levo refuses a sale whose smallest purchase would pay the treasury less than that, and a purchase that would. Match it to the node if it was built with another figure. |
| `LEVOD_CHAIN_TTL` | `2` | Seconds the node's tip may be reused. Set it to 0 on a chain that mines on demand, where a block can arrive and be acted on in the same second. |
| `LEVOD_AUTH_PER_MINUTE` / `LEVOD_WRITES_PER_MINUTE` / `LEVOD_READS_PER_MINUTE` | `30` / `120` / `600` | Per-client limits on signing in, on the calls that write, and on reads. Health is never limited. |
| `LEVOD_VERBOSE` | — | Log every request, including successful health checks and asset fetches. |
| `LEVOD_CHAIN` | — | What to call the chain when the node cannot be asked. levod asks the node and keeps asking until it answers, so this is only a fallback. |
| `LEVOD_ORIGIN` | the request's `Host` | The address this Levo is reached at. It is named in the statement a wallet is asked to sign, so behind a proxy set it to the public URL. |
| `LEVOD_SOURCE_URL` | this repository | Where this Levo's source is. Linked from the site, because a visitor is told to run a command and to rebuild an address themselves. |
| `LEVOD_OPERATORS` | — | Public keys (compressed hex, comma or space separated) that may hide or flag a listing. Empty means nobody can. |
| `LEVOD_TRUSTED_PROXIES` | `127.0.0.1 ::1` | Peers whose `X-Forwarded-For` is believed, and only its **last** entry, which is the one such a proxy appends. Set it empty when levod is exposed directly, or any caller can pick their own rate-limit bucket. |

Every sale's covenant is priced in the asset it was listed with, whatever
`LEVOD_PAYMENT_ASSET` says later; changing it moves only what the form
suggests and what the tier caps are written in.

## What levod can and cannot do

It **cannot move funds**. It holds no keys and signs nothing; the transactions
it builds are unsigned, and only the buyer's (or the project's) wallet can
complete them. Sale tokens sit in a covenant whose leaves do not mention Levo.

It **can mislead**. A hostile or broken levod could show a sale that is not
funded, quote a price that is not the covenant's, or hide a listing. That is why
every sale publishes the terms its address was derived from: rebuild the address
from them and compare it to the funded output, and the only thing that matters
has been checked without trusting the server.
