# Levo

A launchpad on Sequentia: stake sets your allocation ceiling, and a covenant
holds the project's tokens from lock to delivery.

`README.md`'s "What is real" section is the most important thing in the repo.
The sale covenant, the signed-message login and the stake tiers are real and
enforced by consensus. Per-buyer tier caps are Levo's policy and are not.
Never blur that line, in code or in copy.

Node and consensus conventions live in the
[`Sequentia`](https://github.com/ConcatenaLabs/Sequentia) repo.
Levo is a separate project and shares no code with any other platform in the
ecosystem.

## Two halves

| Path | What |
|---|---|
| `levod/` | The backend. Pure Python, standard library only, no dependencies. Serves the API and the built SPA from one origin, so one proxy route covers both. |
| `web/` | Vite + React + plain CSS. No component library and no Tailwind: the visual identity is the point. |

```sh
python3 levod/tests/run.py        # unit checks
python3 levod/tests/test_e2e.py   # the API end to end, against a stub node
python3 levod/demo.py             # the whole platform, no chain needed
cd web && npm install && npm run build
```

There is no CI. Those commands are the whole gate.

## The custody line

levod holds no keys and signs nothing; the transactions it builds are unsigned,
and only the buyer's (or the project's) wallet can complete them. It reads the
chain over JSON-RPC and writes a JSON file of listings. Do not add a route that
accepts key material, and do not add a wallet call to `rpc.py` — the absence of
one is why a compromised levod can mislead but cannot rob.

## Traps

- **`levod/vectors.json` is frozen.** `covenant.py` compares itself to it on
  every import. If that check fails, sale addresses have MOVED, and tokens
  locked under the old bytes are not at the address the new bytes derive. Never
  regenerate the vectors to make a failing check pass; that is a migration, and
  existing sales must be closed out under the old bytes first.
- **The internal key must be NUMS.** Any other internal key gives the project a
  taproot key path, and therefore a way to spend a live sale out from under its
  buyers. `SaleCovenant` refuses to build one; keep it that way.
- **Reduce prices before deriving an address.** `ceil(n*num/den)` is unchanged
  by reducing the fraction, but the leaf's 64-bit arithmetic aborts on overflow
  and forms `filled * num`, so `25000000/100000000` overflows a sale a quarter
  the size that `1/4` handles. `market.list_project` canonicalises. Note that
  the two forms derive DIFFERENT addresses, so clients must verify against the
  published terms, not the ones they submitted.
- **A reorged lock is not a lost confirmation.** Sequentia follows its Bitcoin
  anchor, so funding can be un-made after a sale showed as live. That is `GHOST`:
  the sale is not funded and must stop being investable. Detect it by the BLOCK
  going missing, never by the transaction being unfindable -- a node without
  `-txindex` cannot find a fully spent transaction either, and treating that as
  a reorg reports a sold-out sale as one that was never funded.
- **Asset ids reverse onto the wire.** Levo carries the display form (what an
  explorer shows) and reverses at two boundaries only: covenant leaf constants
  and transaction output commitments. Getting it wrong builds a sale priced in
  an asset nobody holds, silently.
- **Everything a covenant touches must be explicit.** Confidential inputs cannot
  fill a sale, and tokens locked into a confidential output can never be sold or
  reclaimed. Both are refused with reasons; do not soften either.
- **`scantxoutset` sees only CONFIRMED outputs.** Ask about a known outpoint with
  `gettxout` first, and never end a sale on one silent reading -- a mempool
  remainder is invisible to the scan.
- **Anything that has already happened on chain must not be reported as a
  failure.** Recording a purchase is bookkeeping; the money moved before it was
  called.
- **Read stake by CONTROLLER, never by signer.** `getstakerinfo`'s default view
  is keyed by signer, under which a delegator has no stake and their pool has
  everyone's. Levo asks whose money it is, so it uses `bycontroller`. Reverting
  that silently excludes every delegator and hands pools a tier they did not
  pay for.
- **Levo carries its own crypto.** `secp256k1.py` and `script.py` exist so the
  backend needs no node source checkout. They are verified against the node's
  own `signmessage` vector and the frozen covenant vectors. If you change them,
  those tests are the only thing standing between a subtle bug and a wrong
  address.

## Secrets

The repository is public. Never commit keys, seeds, `wallet.dat`, RPC
credentials, `.env` files or tokens. `LEVOD_SECRET` and node credentials are
supplied through the environment on the server, never through the repo.

## Working in this repo

- **Commit author:**
  `GracedEternalKingCabbageMan <151803062+GracedEternalKingCabbageMan@users.noreply.github.com>`
- **Always open a pull request, then merge it yourself immediately.** The PR
  records the change and its reasoning; nobody is waiting to review it.
- Deployment is pull-only: the server pulls from GitHub and builds there. Never
  edit source on the server and never copy binaries onto it.

<!-- BEGIN SHARED AGENT CONVENTIONS: identical in every Sequentia repo. Change it in all of them together. -->
## Working with git and GitHub here

These rules are the same in every Sequentia repository. They are repeated in each
one because this file is the only thing an agent is guaranteed to read, whatever
machine it is working from.

**Nothing pushed to GitHub credits Claude, Anthropic, or any AI tool.** No
`Co-Authored-By: Claude` trailer, no `Claude-Session:` trailer or `claude.ai`
link, no "Generated with Claude Code" in a commit message or a pull request body,
no `claude/*` branch names or session ids, and no mention in source, comments,
docs or issue text. Agent tooling offers several of these by default; compose the
message without them rather than stripping them afterwards.

**Author every commit as**
`GracedEternalKingCabbageMan <151803062+GracedEternalKingCabbageMan@users.noreply.github.com>`.
Never a personal address.

**Every change lands through a pull request that you merge yourself, at once.**
There is no reviewer on this project; the pull request exists so the reasoning is
recorded beside the diff. Branch, push, open it, merge it, delete the branch, all
in one sitting. Pushing straight to the default branch is the rule most often
broken here, and it is the one that costs the record. A pull request stays open
only when the repository owner asks for that specific one, and that never carries
over to the next.

**Name branches `area/short-description`**: `fix/`, `doc/`, `feature/`, `test/`,
`build/`, or the component being changed. Never a tool name, a session id, or
`worktree-*`.

**Write the subject as `area: what changed`**, one line, 72 characters at the
outside and 50 where you can manage it. Put the reasoning in the body, and
explain why rather than what.

**These repositories are public and world-readable.** Never commit private keys,
seeds, `wallet.dat`, RPC credentials, `.env` files or API tokens. Read the diff
before every commit. Secrets belong on the server and in offline backups.

**A file belongs to the repository whose code it describes.** Decide which repo
owns it before writing it; if it landed in the wrong one, move it rather than
deleting it.

**Push the same day you commit.** The testnet server pulls only from GitHub, so a
branch left on one laptop is invisible to every other machine and to the box.
<!-- END SHARED AGENT CONVENTIONS -->
