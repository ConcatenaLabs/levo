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
- **Tiers from live stake.** Stake weight comes from the node's `getstakerinfo`,
  counted only for staker keys an account has proven it controls.

One thing is deliberately **not** consensus, and the code says so everywhere it
appears: **per-buyer tier caps are Levo's allocation policy, not a chain rule.**
The sell leaf has a minimum lot but no maximum, and it is permissionless by
design. See [doc/tiers-are-policy.md](doc/tiers-are-policy.md).

The **BTC rail** is specified but not wired to a Lightning provider in this
tree; `levod/rails.py` reports it unavailable until one is configured. USDX
works end to end.

## Layout

| Path | What |
|---|---|
| `levod/` | The backend. Pure Python, standard library only. Serves the API and the built app from one origin. |
| `levod/covenant.py` | The sale covenant, checked byte for byte against `levod/vectors.json` on every import. |
| `levod/secp256k1.py`, `levod/script.py` | Curve and script primitives. Levo carries its own so it needs no node source checkout. |
| `web/` | The single-page app: Vite and React, plain CSS. |
| `doc/` | The design notes worth keeping outside the code. |

## Running it

```sh
cd web && npm install && npm run build     # build the app once
python3 levod/demo.py                      # then open http://127.0.0.1:8099
```

`demo.py` replaces the node with a stub and seeds two sales, so the whole
platform can be clicked through without a chain. Covenant addresses, signature
recovery and tier arithmetic are the shipped code; only the node is faked.

Against a real node:

```sh
export LEVOD_RPC_URL=http://127.0.0.1:7041
export LEVOD_RPC_COOKIE=~/.sequentia/testnet/.cookie   # or LEVOD_RPC_USER/PASSWORD
export LEVOD_SECRET=$(head -c32 /dev/urandom | xxd -p -c64)
python3 levod/server.py
```

## Tests

```sh
python3 levod/tests/run.py        # 106 checks: crypto, covenant, tiers
python3 levod/tests/test_e2e.py   #  43 checks: the API end to end
cd web && npm run build           # the frontend gate
```

No CI, no framework, no network. Those three commands are the whole gate.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LEVOD_HOST` / `LEVOD_PORT` | `127.0.0.1:8099` | Where levod listens. |
| `LEVOD_WEBROOT` | `web/dist` | The built app. |
| `LEVOD_STATE` | `levo-state.json` | Listings and the allocation ledger. |
| `LEVOD_SECRET` | random per start | Session-token key. Set it, or every restart signs everyone out. |
| `LEVOD_RPC_URL` | `http://127.0.0.1:7041` | Sequentia node JSON-RPC. |
| `LEVOD_RPC_USER` / `LEVOD_RPC_PASSWORD` / `LEVOD_RPC_COOKIE` | — | Node credentials. |
| `LEVOD_PAYMENT_ASSET` | USDX on testnet | The asset sales are priced in. |

## What levod can and cannot do

It **cannot move funds**. It holds no keys, builds no transactions and signs
nothing. Sale tokens sit in a covenant whose leaves do not mention Levo.

It **can mislead**. A hostile or broken levod could show a sale that is not
funded, quote a price that is not the covenant's, or hide a listing. That is why
every sale publishes the terms its address was derived from: rebuild the address
from them and compare it to the funded output, and the only thing that matters
has been checked without trusting the server.
