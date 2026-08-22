# Levo

A launchpad on [Sequentia](https://github.com/GracedEternalKingCabbageMan/Sequentia).

Levo allocates by commitment. Stake Sequence and you can take a position in a
sale; stake enough and you can run one. Everything in between is settled by a
covenant that holds the project's tokens from the moment they are locked to the
moment they reach a buyer.

*Levo* is Latin for to raise, to lift. The platform's one drawing is a balance
beam, because the rule it encodes is literally that committed weight sets the
ceiling.

## What is real

Everything that protects money is real and enforced by consensus:

- **Sale covenants.** A sale is a taproot output with a NUMS internal key and
  two leaves. Tokens can leave it by being sold at the published price, or by
  being reclaimed by the project after the close date. Nothing else.
- **Signed-message login.** An account is a public key. Levo issues a challenge,
  a wallet signs it, and Levo recovers the key from the signature.
- **Tiers from live stake, delegated or not.** Weight comes from the node,
  counted only for staker keys an account has proven it controls -- and read by
  CONTROLLER rather than by signer, so a stake delegated to a pool still counts
  for the person who owns it. See [doc/delegated-stake.md](doc/delegated-stake.md).

- **Settlement.** Levo builds the transaction that spends the covenant, pays the
  treasury and delivers the tokens. It signs nothing and holds no keys.
- **The chain decides.** A watcher reconciles every sale against the UTXO set,
  so a purchase made without Levo still moves the sale, and a lock undone by a
  Bitcoin-driven reorg stops being investable.

One thing is deliberately **not** consensus, and the code says so everywhere it
appears: **per-buyer tier caps are Levo's allocation policy, not a chain rule.**
The sell leaf has a minimum lot but no maximum, and it is permissionless by
design. See [doc/tiers-are-policy.md](doc/tiers-are-policy.md).

## Proven on chain

Both covenant paths were exercised on the Sequentia testnet, and the platform
has run a sale end to end at
[sequentiatestnet.com/levo](https://sequentiatestnet.com/levo/):

| What | Transaction |
|---|---|
| A buy: treasury paid, remainder re-rested at the identical address | `bbed75291600bcd31ef9f6db4b2aaa4466a6a8399d66f8f1f6ec2b20a286ce69` |
| A reclaim after the close, via the reclaim leaf | `2f97173f4dd60976e5862f0bb572871114c6008607eb16003911f4eff1b843ab` |
| A buy through the deployed platform, driven by `bin/levo` | `555dfef9b5a783dbb0180ad2c01fd27cd5188a8f5eaea27f997b365330ea4c03` |

## Paying

**USDX** settles inside the covenant: buyer and project exchange in one
transaction, enforced by consensus.

**BTC** is native Bitcoin on the parent chain, not a token on Sequentia and not
a pegged claim on one, so a Sequentia covenant cannot read a Bitcoin output. A
BTC purchase is two atomic steps -- swap to the payment asset over Lightning,
then fill the covenant -- with the buyer holding the payment asset in between.
Levo takes custody of neither leg. Quotes come from the chain's own
`getfeeexchangerates`, the table the network uses to price fees in any asset, so
Levo cannot quote a rate the chain disagrees with.

## Layout

| Path | What |
|---|---|
| `levod/` | The backend. Pure Python, standard library only. Serves the API and the built app from one origin. |
| `levod/server.py` | The service: the API under `/api/` and the built app, one origin. `levod/demo.py` is the same server over a stub node. |
| `levod/covenant.py` | The sale covenant, checked byte for byte against `levod/vectors.json` on every import. |
| `levod/sale.py`, `levod/market.py` | The sale lifecycle (lock, sell, reclaim) and the marketplace rules: listing, the settlement plan, the per-buyer caps. |
| `levod/tiers.py` | The tier policy, and the `LEVOD_TIERS` table it reads. |
| `levod/rails.py` | The USDX and BTC payment rails and their quotes, priced from `getfeeexchangerates`. |
| `levod/auth.py` | Login: a challenge, a signature, and the key recovered from it. |
| `levod/store.py` | The state file: listings and the allocation ledger, written atomically. |
| `levod/tx.py` | Builds the transaction that settles a buy, and the one that reclaims what did not sell. |
| `levod/watcher.py` | Reconciles sales against the UTXO set; the chain is the source of truth. |
| `levod/secp256k1.py`, `levod/script.py`, `levod/address.py` | Curve, script and address primitives. Levo carries its own so it needs no node source checkout. |
| `tools/gen_vectors.py` | Regenerates `levod/vectors.json`. Running it is a migration, not a refresh; see `CLAUDE.md`. |
| `bin/levo` | A CLI that runs the whole flow against your own node. |
| `web/` | The single-page app: Vite and React, plain CSS. |
| `doc/` | The design notes worth keeping outside the code. |

## Running it

```sh
cd web && npm install && npm run build     # build the app once
python3 levod/demo.py                      # then open http://127.0.0.1:8099
```

The app builds with **Node 20.19+ or 22.12+** (Vite's engine range). On an older Node the native
bundler binding is skipped as an unmet engine and the build fails on a missing
module rather than on the version, which is a confusing way to find out.

Serving from a sub-path (`/levo/` behind a reverse proxy that strips the
prefix) needs `LEVO_BASE=/levo/ npm run build`; the app reads its own base at
runtime, so nothing else changes.

`demo.py` replaces the node with a stub and seeds two sales, so the whole
platform can be clicked through without a chain. Covenant addresses, signature
recovery and tier arithmetic are the shipped code; only the node is faked.

With a node of your own, `bin/levo` runs the real thing:

```sh
export LEVO_URL=https://sequentiatestnet.com/levo
levo sales
levo buy helios-grid --tokens 40      # picks inputs, builds, signs, broadcasts
```

Against a real node:

```sh
export LEVOD_RPC_URL=http://127.0.0.1:18776            # the testnet's RPC port
export LEVOD_RPC_COOKIE=~/.elements/testnet3/.cookie    # or LEVOD_RPC_USER/PASSWORD
export LEVOD_SECRET=$(head -c32 /dev/urandom | xxd -p -c64)
python3 levod/server.py
```

## Tests

```sh
python3 levod/tests/run.py        # 199 checks: crypto, covenant, tiers, transactions, watcher
python3 levod/tests/test_e2e.py   #  78 checks: the API end to end
cd web && npm run build           # the frontend gate
```

Schnorr signing is checked against the official BIP340 vectors, the transaction
serialisation against a txid a live node computed, and the address encoder
against an address a live node printed.

No CI, no framework, no network. Those three commands are the whole gate.

## Deployment

The platform is live at [sequentiatestnet.com/levo/](https://sequentiatestnet.com/levo/).
The box pulls this repo from GitHub and runs `levod/server.py` under systemd;
`LEVOD_SECRET` and the node credentials are supplied through a systemd drop-in,
never through the repo. The app is built with
`LEVO_BASE=/levo/ npm run build`, and Caddy routes the sub-path to levod's
default port, stripping the prefix:

```
handle_path /levo/* {
    reverse_proxy 127.0.0.1:8099
}
```

One route covers both the API (`/levo/api/...`) and the app, because levod
serves them from one origin.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LEVOD_HOST` / `LEVOD_PORT` | `127.0.0.1:8099` | Where levod listens. |
| `LEVOD_WEBROOT` | `web/dist` | The built app. |
| `LEVOD_STATE` | `levo-state.json` | Listings and the allocation ledger. |
| `LEVOD_SECRET` | random per start | Session-token key. Set it, or every restart signs everyone out. |
| `LEVOD_RPC_URL` | `http://127.0.0.1:18776` | Sequentia node JSON-RPC. The default is the node's RPC port on chain `test`; the testnet box points it at its own follower node. |
| `LEVOD_RPC_USER` / `LEVOD_RPC_PASSWORD` / `LEVOD_RPC_COOKIE` | — | Node credentials. |
| `LEVOD_PAYMENT_ASSET` | USDX on testnet | The asset sales are priced in. |
| `LEVOD_PAYMENT_LABEL` | `USDX` | Its label in the node's rate table. |
| `LEVOD_HRP` | `tb` | Address prefix: `tb` testnet, `bc` mainnet (Sequentia's unblinded addresses use Bitcoin's own HRPs), `ert` on `elementsregtest`. |
| `LEVOD_TIERS` | supply-share defaults | JSON tier table; see `levod/tiers.py`. |
| `LEVOD_WATCH_SECONDS` | `60` | How often the watcher reconciles. |

## What levod can and cannot do

It **cannot move funds**. It holds no keys and signs nothing; the transactions
it builds are unsigned, and only the buyer's (or the project's) wallet can
complete them. Sale tokens sit in a covenant whose leaves do not mention Levo.

It **can mislead**. A hostile or broken levod could show a sale that is not
funded, quote a price that is not the covenant's, or hide a listing. That is why
every sale publishes the terms its address was derived from: rebuild the address
from them and compare it to the funded output, and the only thing that matters
has been checked without trusting the server.
