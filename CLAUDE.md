# Levo

A launchpad on Sequentia: stake sets your allocation ceiling, and a covenant
holds the project's tokens from lock to delivery.

`README.md`'s "What is real" section is the most important thing in the repo.
The sale covenant is enforced by consensus; the signed-message login and the
stake weights are real facts Levo checks against a signature and the chain.
Per-buyer tier caps and the tier boundaries are Levo's policy. The close date
opens the reclaim path and does not shut the sell path, and a project can
always buy its own sale out at the published price. Never blur any of those
lines, in code or in copy.

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
python3 levod/tests/test_node.py  # against a real sequentiad; skipped without one
npm --prefix web test && npm --prefix web run build
python3 levod/demo.py             # the whole platform, no chain needed
```

There is no CI. Those commands are the whole gate. `test_node.py` is the only
one that proves anything about consensus; run it whenever `covenant.py`,
`tx.py`, `pset.py` or `watcher.py` change.

## The custody line

levod holds no keys and signs nothing; the transactions it builds are unsigned,
and only the buyer's (or the project's) wallet can complete them. It reads the
chain over JSON-RPC and writes a JSON file of listings. Do not add a route that
accepts key material, do not add a signing path to `tx.py` (the reclaim returns
a sighash; `bin/levo` signs it on the project's machine), and do not add a
wallet call to `rpc.py` — the absence of those is why a compromised levod can
mislead but cannot rob.

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
  fill a sale, output 1 of a buy must be explicit (the leaf inspects its asset),
  and tokens locked into a confidential output can never be sold, only taken
  back by the project after the close. All are refused with reasons; do not
  soften any of them.
- **The sell leaf does not pin the asset it spends.** Anything resting at a sale
  address can be bought at the sale's price, so the watcher reports stray assets
  and the lock instructions say to send nothing but the token. A leaf that
  checks the input's asset moves every sale address: a vectors migration.
- **A sale ends only after a new block says so.** The watcher counts silent
  polls AND requires the chain to have moved, because a remainder in the mempool
  is invisible to the confirmed-set scan; a recorded purchase names the outpoint
  the remainder rests at so it is seen at once. GHOST and SOLD_OUT are final;
  CLOSED-and-empty becomes RECLAIMED only on the reclaim's own output.
- **The ledger only grows, and only by named transactions.** `record_purchase`
  needs a txid, positive amounts, and never records less than the covenant's
  price for the tokens named. A repeat of the same txid is answered, not added.
- **A signed statement must be the issued statement.** Login and stake links
  compare the whole text, not just the nonce; a caller may name the address it
  signed with so a signature over different bytes is an error, not a phantom
  account. Statements end without a newline so shells and text boxes agree.
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

**Documentation is part of the change, not a follow-up.** A change that makes a
README, a doc page, a runbook or a code comment wrong is not finished until that
text is right again, in the same pull request as the code. Before you open the
pull request, search the repository for whatever you renamed, moved or removed —
the old binary name, the old path, the old flag, the old command — and fix every
hit. If the change falsifies another repository's documentation, that repository
gets its own pull request in the same sitting. A stale instruction costs a new
user more than a missing one: they trust it, run it, it fails, and the failure
reads as broken software rather than as an out-of-date sentence.

**Write documentation to be timeless.** Assume the reader is new, arrived today,
and wants to know what the software is and how to use it right now. They do not
care what changed, what it used to be called, or which version added what. So
write in the present tense about current behaviour, and leave the history out:
no changelogs, no "new in", no "recently", no "coming soon", no status or
progress sections, no roadmaps, no dated notes. Quote a version number only where
the reader cannot act without it, and prefer pointing at the file that carries it
over copying the digits. Timeless does not mean thin — what the product is, who
it is for, and how to install, configure and use it all still belong there, in
full. Documentation written this way survives a release without an edit, which is
what keeps it true; the history already has homes in the git log, the tags and
the release notes.

**Push the same day you commit.** The testnet server pulls only from GitHub, so a
branch left on one laptop is invisible to every other machine and to the box.
<!-- END SHARED AGENT CONVENTIONS -->
