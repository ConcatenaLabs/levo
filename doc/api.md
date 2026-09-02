# The API

levod serves this under `/api/` from the same origin as the app, so a
deployment behind a reverse proxy needs one route for both. Everything is JSON.
Amounts are always **atoms** — the asset's smallest unit — and are sent and
returned as decimal strings or numbers; a client that cannot hold a number
above 2^53 should read them as strings, which levod accepts on the way in.

An error is `{"error": "<a sentence>"}` with a status:

| Status | Means |
|---|---|
| 400 | the request is wrong, and the sentence says how |
| 401 | this route needs a session and there is none |
| 403 | a session that is not allowed to do this |
| 404 | no such sale, or no such route |
| 405 | that path exists and does not take that method; `Allow` lists what it does |
| 409 | the purchase is larger than the caller's remaining cap; carries `allowance_atoms` |
| 429 | too many requests from this address; `Retry-After` says how long to wait |
| 502 | the Sequentia node could not be reached or refused the query |
| 503 | levod is busy, or unhealthy; `Retry-After` where it makes sense |

## Signing in

An account is a public key. There is no password and no registration.

**`POST /api/auth/challenge`** → `{nonce, message, expires_at}`. The message
names the site, carries a single-use nonce, and says in its own text that it
authorises no payment. Sign it exactly as issued.

**`POST /api/auth/verify`** `{message, signature, address?}` → `{token, account,
…}`. `signature` is a base64 recoverable signature over the message, as
`sequentia-cli signmessage` and every wallet's sign-message produces. `address`
is optional and checked when given: a signature over slightly different bytes
recovers to a key nobody holds, and naming the address turns that into an error
rather than a phantom account.

Send the token as `Authorization: Bearer <token>` on every route below that
says it needs a session.

## Stake

**`GET /api/me`** (session) → the caller's standing: `account`, `stake_atoms`,
`tier`, the keys it has proved, `operator` (whether it may flag a listing).

**`POST /api/stake/challenge`** `{staker_pubkey}` (session) → a statement
naming both the account and the staking key, so a signature collected for one
purpose cannot be replayed to attach the same stake elsewhere.

**`POST /api/stake/link`** `{message, signature, staker_pubkey}` (session) →
the new standing. **`POST /api/stake/unlink`** `{staker_pubkey}` (session)
removes it.

**`GET /api/me/projects`** (session) → the caller's own listings, each with its
lock instructions while it is still a draft. **`GET /api/me/positions`**
(session) → what the caller has bought in each sale, what it has committed
against its cap, and what it may still commit. Both take `limit` and `offset`
and answer with `total`, and a position carries its most recent purchases with
`purchases_total` beside them.

## The board

**`GET /api/projects`** → `{projects, total, offset, limit, status, sort,
query, node_reachable}`.

| Parameter | Values |
|---|---|
| `status` | `open`, `finished`, `draft`, `all` (default `all`) |
| `sort` | `new`, `closing`, `progress` (default `new`) |
| `q` | matches a listing's page name, name, ticker or summary |
| `limit`, `offset` | a page; a size is always applied, and `total` says how many there are |

**`GET /api/projects/<slug>`** → the listing, its sale, its address, and
`verify`: the leaves and the internal key, so a client can rebuild the address
from the terms and compare. That comparison is the only thing that has to be
trusted, and it does not have to be trusted to levod.

**`GET /api/projects/<slug>/fee`** `?kind=buy|reclaim&inputs=<n>&asset=<id>` →
what a fee should be for a transaction of that shape, from the node's own relay
floor: `{asset, vsize_estimate, min_atoms, suggested_atoms, rate_atoms_per_kvb}`.

## Listing a sale

**`POST /api/projects`** `{project, terms}` (session, a tier that may list) →
`{project, lock}`. `lock` says where to send the tokens and how many.

`project`: `slug`, `name`, `ticker`, `decimals`, `summary`, `description`,
`links`. `terms`: `token_asset`, `price_num`, `price_den`, `min_lot`,
`total_atoms`, `close_locktime`, `reclaim_xonly`, and the treasury as either
`treasury_address` or `treasury_prog` (+ `treasury_ver`). The price is stored
in lowest terms, so the response is what the address was derived from.

**`PATCH /api/projects/<slug>`** (session, the issuer) edits the copy.
**`DELETE /api/projects/<slug>`** withdraws a listing that was never funded.

**`POST /api/projects/<slug>/lock`** `{txid?, vout?}` (session, the issuer) →
the sale page's own shape. Without an outpoint levod scans the confirmed set
for the sale's address; with one it can confirm a lock still in the mempool.

**`POST /api/projects/<slug>/reclaim`** `{destination_address, fee_inputs,
fee_atoms, fee_asset?}` (session, the issuer, after the close) → the unsigned
transaction, the `sighash` to sign with the reclaim key, the leaf and the
control block. levod never sees a key.

**`GET /api/projects/<slug>/purchases`** (session, the issuer or an operator) →
Levo's own ledger for that sale, newest first, with `limit`, `offset` and
`total`, and what each account has committed.

## Buying

**`POST /api/projects/<slug>/buy`** `{token_atoms | payment_atoms, rail?}`
(session) → the plan: what it costs at the covenant's own ceiling price, what
is left after it, the caller's cap, fee advice, and a quote when the rail is
BTC.

**`POST /api/outputs/check`** `{outputs: [{txid, vout}]}` (session) → for each,
whether a covenant purchase can spend it, with the asset and amount. A
confidential output commits to its value instead of stating one, and the sell
leaf reads the value it spends, so it cannot fund a purchase.

**`POST /api/projects/<slug>/transaction`** `{token_atoms, buyer}` (session) →
the unsigned transaction as hex and as a PSET, with every output described.
`buyer` carries `token_address` (or `token_script_pubkey`), optional
`change_address`, `inputs`, `fee_atoms` and optional `fee_asset`. levod signs
nothing: the covenant input needs no signature and the buyer's inputs can only
be signed by the buyer.

**`POST /api/projects/<slug>/confirm`** `{txid, token_atoms, payment_atoms}`
(session) records the purchase against the caller's cap. This writes Levo's
ledger and nothing else — the sale itself moves because the chain moved, whether
or not anything was recorded here. levod refuses a transaction its node has
never seen, and takes the amount from the treasury credit on chain when it can
still read it.

## Operating

**`GET /api/health`** → `{ok, node, watcher, state_file, app}`. `ok` is false
when the node is unreachable, the watcher has stalled or is failing every poll,
the state file cannot be written, or the built app has gone missing. This is the
endpoint to point an uptime check at.

**`GET /api/watcher`** → what the watcher is doing, and any sale whose funding
it cannot place in the chain.

**`GET /api/config`**, **`GET /api/tiers`**, **`GET /api/rails`** → the labels,
prefixes, links, tier table and payment rails this deployment runs with,
including how many decimal places the payment asset divides into. They
change rarely and may be held for half a minute.

**`POST /api/projects/<slug>/flag`** `{hidden?, notice?}` (session, an account
in `LEVOD_OPERATORS`) takes a listing off the board or puts a notice on its
page. It reaches the page and nothing else: the sale is a covenant on a public
chain, and anyone holding its terms can still buy from it.
