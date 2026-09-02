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
  project from the close date on. Nothing else.
- **Settlement**, enforced by consensus. A buy spends the covenant, pays the
  treasury and delivers the tokens in one transaction. Levo builds that
  transaction unsigned; only the buyer's wallet can complete it.
- **Signed-message login**, a signature Levo checks. An account is a public key.
  Levo issues a challenge, a wallet signs it, and Levo recovers the key from the
  signature. The signed text has to be the issued challenge, word for word.
- **Stake, read from the chain.** The weight behind each key comes from the
  node, counted only for staker keys an account has proven it controls, and
  read by CONTROLLER rather than by signer, so a stake delegated to a pool still
  counts for the person who owns it. See [doc/delegated-stake.md](doc/delegated-stake.md).
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
they appear. **Per-buyer tier caps are Levo's allocation policy.** The sell leaf
has a minimum lot but no maximum, and it is permissionless by design; see
[doc/tiers-are-policy.md](doc/tiers-are-policy.md). **The close date opens the
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
([`bbed7529…ce69`](https://sequentiatestnet.com/explorer/tx/bbed75291600bcd31ef9f6db4b2aaa4466a6a8399d66f8f1f6ec2b20a286ce69)),
and a reclaim through the reclaim leaf after the close
([`2f97173f…3ab`](https://sequentiatestnet.com/explorer/tx/2f97173f4dd60976e5862f0bb572871114c6008607eb16003911f4eff1b843ab)).
The platform runs sales end to end at
[sequentiatestnet.com/levo](https://sequentiatestnet.com/levo/). You do not
have to take that on trust: every sale publishes the terms its address was
derived from, so rebuild the address and compare it to the funded output.
`bin/levo verify <sale>` does exactly that against your own node.

## Paying

**USDX** settles inside the covenant: buyer and project exchange in one
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

## Who does what

**A buyer** needs a Levo account (a key that can sign a message: the browser
extension, or any wallet that signs messages), staked Sequence under a key they
can prove they control, and unblinded USDX. A browser wallet signs and
broadcasts the purchase in place: Levo hands it a PSET whose covenant input
already carries its witness, and the wallet signs only the buyer's own inputs.
That needs a wallet that fills in its own key origins before signing, which it
announces as the `pset-site-built` capability; where the wallet cannot, Levo
offers the node path instead and says so. A node signs the same purchase with
`signrawtransactionwithwallet`, or lets `bin/levo buy` do the whole thing.

**A project** needs a tier that may list, an issued asset (registered, so
wallets show its name), the whole allocation in a wallet it can send from, an
address for the treasury, and a reclaim key it can sign with outside a browser
wallet, because reclaiming means signing a raw sighash. `bin/levo keygen` makes
one. The treasury may be any witness address the project's wallet hands out,
taproot or version-0: the version is compiled into the leaf beside the program,
so a wallet without taproot addresses can still run a sale. The project locks
its tokens by sending them to the sale address, and Levo finds the lock on
chain; after the close, `bin/levo reclaim` sweeps what did not sell.

## Layout

| Path | What |
|---|---|
| `levod/` | The backend. Pure Python, standard library only. Serves the API and the built app from one origin. |
| `levod/server.py` | The service: the API under `/api/` and the built app, one origin. `levod/demo.py` is the same server over a stub node, with two seeded sales. |
| `levod/covenant.py` | The sale covenant, checked byte for byte against `levod/vectors.json` on every import. |
| `levod/sale.py`, `levod/market.py` | The sale lifecycle (lock, sell, reclaim) and the marketplace rules: listing, the settlement plan, the per-buyer caps, the ledger. |
| `levod/tiers.py` | The tier policy, and the `LEVOD_TIERS` table it reads. |
| `levod/rails.py` | The USDX and BTC payment rails and their quotes. |
| `levod/auth.py` | Login: a challenge, a signature, and the key recovered from it. |
| `levod/store.py` | The state file: listings and the allocation ledger, written atomically. |
| `levod/tx.py`, `levod/pset.py` | The transaction that settles a buy (as raw hex and as a PSET), and the one that reclaims what did not sell. |
| `levod/watcher.py` | Reconciles sales against the UTXO set and the mempool; the chain is the source of truth. |
| `levod/secp256k1.py`, `levod/script.py`, `levod/address.py` | Curve, script and address primitives. Levo carries its own so it needs no node source checkout. |
| `tools/gen_vectors.py` | Regenerates `levod/vectors.json`. Running it is a migration, not a refresh; see `CLAUDE.md`. |
| `bin/levo` | A CLI that runs the whole flow against your own node. |
| `web/` | The single-page app: Vite and React, plain CSS, fonts served from the app itself. |
| `contrib/` | The systemd unit, an environment file to fill in, and a backup timer for the state file. |
| `doc/` | The design notes worth keeping outside the code, and `doc/api.md`, the HTTP API. |

## Running it

```sh
npm --prefix web install && npm --prefix web run build   # build the app once
python3 levod/demo.py                                    # then open http://127.0.0.1:8099
```

The app builds with **Node 20.19 or later, or 22.12 or later** (Vite's engine
range). On an older Node the native bundler binding is skipped as an unmet
engine and the build fails on a missing module rather than on the version,
which is a confusing way to find out.

Serving from a sub-path (`/levo/` behind a reverse proxy that strips the
prefix) needs `LEVO_BASE=/levo/ npm run build`; the app reads its own base at
runtime, so nothing else changes.

`demo.py` replaces the node with a stub and seeds two sales, so the whole
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
| `SEQUENTIA_CLI` | The node's CLI binary. Default `sequentia-cli` on `PATH`. |
| `SEQUENTIA_DATADIR` | Passed as `-datadir`, for a node in a non-default place. |
| `SEQUENTIA_WALLET` | Passed as `-rpcwallet`, when several wallets are loaded. |
| `LEVO_SIGN_WIF` | Sign in with this key instead of a wallet address. A staker who signs in with the staking key's WIF is recognised at once; there is nothing to link. Without it, the first sign-in creates a legacy address labelled `levo-login` and every later sign-in reuses it, so the account stays the same key. The node's message signing works with legacy addresses only. |
| `LEVO_RECLAIM_KEY` | The 32-byte hex reclaim secret, for `levo reclaim` (or pass `--reclaim-key`). |

`levo --help` lists every command: `sales`, `show`, `verify`, `whoami`,
`link`, `keygen`, `create`, `lock`, `buy`, `reclaim`, `withdraw`, and `flag`
for an operator. Fees are
never defaulted to the policy asset: `lock`, `buy` and `reclaim` pay them in the
sale's payment asset unless told otherwise, and their size comes from the
node's own relay floor rather than a figure typed in.

Listing from the command line takes a JSON file holding `{"project": {...},
"terms": {...}}`. `bin/levo create --example` prints one to start from, with a
note on what each field means; `bin/levo create listing.json` submits it.

## Tests

```sh
python3 levod/tests/run.py        # crypto, covenant, tiers, transactions, watcher, persistence
python3 levod/tests/test_e2e.py   # the API end to end, against a stub node
python3 levod/tests/test_node.py  # the whole life of a sale against a real sequentiad; skipped without one
python3 levod/tests/test_render.py # every page, in a real browser; skipped without a chromium
npm --prefix web test             # formatting, the beam's geometry, bech32
npm --prefix web run build        # the frontend gate
```

Schnorr signing is checked against the BIP340 vectors, the taproot tweak
against BIP341's, the transaction serialisation against a txid a live node
computed, the address encoder against an address a live node printed, and the
covenant bytes against frozen vectors. The node test starts `sequentiad` on a
throwaway regtest chain (it looks at `SEQUENTIAD`, `SEQUENTIA_SRC/src`, then
`~/Sequentia/src`) and runs a lock, a PSET purchase, a raw purchase, the
watcher, a sell-out, a reclaim under both kinds of close, a sale whose treasury
is a version-0 address, a funding that never lands, and every spend the
covenant must refuse.

The render test starts the demo server and paints every route in a headless
Chromium, failing on a console error or a page that painted nothing. It is
there because every other test reads code or talks to the API: a change can
leave the bundle building and the routes answering and still ship a white
screen.

No CI and no framework. Those commands are the whole gate.

## Deployment

The platform is live at [sequentiatestnet.com/levo/](https://sequentiatestnet.com/levo/).
The box pulls this repo from GitHub and runs `levod/server.py` under systemd;
`contrib/levod.service` is the unit, and `contrib/levod.env.example` lists every
setting. `LEVOD_SECRET` and the node credentials are supplied through the
environment file the unit reads (`EnvironmentFile=`), never through the repo.
The state file lives outside the checkout (`LEVOD_STATE=/var/lib/levo/levo-state.json`)
and `contrib/levo-backup.sh` with its timer keeps dated copies of it: a funded
sale's leaves are rebuilt from the terms in that file, so it is worth keeping.

The app is built on the box with `LEVO_BASE=/levo/ npm run build` under a Node
that meets Vite's engine range, and Caddy routes the sub-path to levod's default
port, stripping the prefix:

```
redir /levo /levo/ permanent
handle_path /levo/* {
    reverse_proxy 127.0.0.1:8099
}
```

One route covers both the API (`/levo/api/...`) and the app, because levod
serves them from one origin. Uptime checks should watch `/levo/api/health`,
which answers 503 when the node is unreachable, when the watcher has stalled,
or when it runs but every poll is failing -- a watcher that reconciles nothing
leaves sold-out sales showing as open.

An operator named in `LEVOD_OPERATORS` can hide a listing from the board and
put a notice on its page, from the sale's own page or with `bin/levo flag`.
That reaches the page and nothing else: the sale is a covenant on a public
chain, and anyone holding its terms can still buy from it.

`contrib/README.md` carries the upgrade and the restore, both as commands. The
upgrade reinstalls the systemd units, because a unit change in this repo
reaches the box nowhere else.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LEVOD_HOST` / `LEVOD_PORT` | `127.0.0.1:8099` | Where levod listens. |
| `LEVOD_WEBROOT` | `web/dist` | The built app. |
| `LEVOD_STATE` | `levo-state.json` | Listings, purchases and the allocation ledger. Use an absolute path outside the checkout. |
| `LEVOD_SECRET` | random per start | Session-token key. Set it, or every restart signs everyone out. |
| `LEVOD_RPC_URL` | `http://127.0.0.1:18776` | Sequentia node JSON-RPC. The default is the node's RPC port on chain `test`; the testnet box points it at its own follower node. |
| `LEVOD_RPC_USER` / `LEVOD_RPC_PASSWORD` / `LEVOD_RPC_COOKIE` | — | Node credentials. The cookie path is tilde-expanded, read on every call, and refused at startup if it does not exist. |
| `LEVOD_PAYMENT_ASSET` | USDX on testnet | The asset every sale is priced in. A listing in another asset is refused. |
| `LEVOD_PAYMENT_LABEL` | `USDX` | Its label in the node's rate table and in the interface. |
| `LEVOD_STAKE_LABEL` | `tSEQ` on chain `test`, else `SEQ` | The staking token's ticker in the interface. |
| `LEVOD_HRP` | `tb` | Address prefix: `tb` testnet, `bc` mainnet (Sequentia's unblinded addresses use Bitcoin's own HRPs), `ert` on `elementsregtest`. |
| `LEVOD_TIERS` | supply-share defaults | JSON tier table; see `levod/tiers.py`. Takes effect on restart; caps apply to open sales at once, while what an account has already committed stays committed. |
| `LEVOD_EXPLORER_URL` | — | An esplora-style explorer base (`.../tx/`, `.../address/`, `.../asset/`), for links. |
| `LEVOD_LINKS` | — | JSON of label to URL for the rest of the deployment (wallet, faucet, staking pools), shown on the site. |
| `LEVOD_WATCH_SECONDS` | `60` | How often the watcher reconciles. |
| `LEVOD_AUTH_PER_MINUTE` / `LEVOD_WRITES_PER_MINUTE` / `LEVOD_READS_PER_MINUTE` | `30` / `120` / `600` | Per-client limits on signing in, on the calls that write, and on reads. Health is never limited. |
| `LEVOD_VERBOSE` | — | Log every request, including successful health checks and asset fetches. |
| `LEVOD_CHAIN` | — | What to call the chain when the node cannot be asked. levod asks the node and keeps asking until it answers, so this is only a fallback. |
| `LEVOD_ORIGIN` | the request's `Host` | The address this Levo is reached at. It is named in the statement a wallet is asked to sign, so behind a proxy set it to the public URL. |
| `LEVOD_SOURCE_URL` | this repository | Where this Levo's source is. Linked from the site, because a visitor is told to run a command and to rebuild an address themselves. |
| `LEVOD_OPERATORS` | — | Public keys (compressed hex, comma or space separated) that may hide or flag a listing. Empty means nobody can. |
| `LEVOD_TRUSTED_PROXIES` | `127.0.0.1 ::1` | Peers whose `X-Forwarded-For` is believed. Set it empty when levod is exposed directly, or any caller can pick their own rate-limit bucket. |

Changing `LEVOD_PAYMENT_ASSET` while a sale is open is not supported: every
sale's covenant is priced in the asset it was listed with.

## What levod can and cannot do

It **cannot move funds**. It holds no keys and signs nothing; the transactions
it builds are unsigned, and only the buyer's (or the project's) wallet can
complete them. Sale tokens sit in a covenant whose leaves do not mention Levo.

It **can mislead**. A hostile or broken levod could show a sale that is not
funded, quote a price that is not the covenant's, or hide a listing. That is why
every sale publishes the terms its address was derived from: rebuild the address
from them and compare it to the funded output, and the only thing that matters
has been checked without trusting the server.
