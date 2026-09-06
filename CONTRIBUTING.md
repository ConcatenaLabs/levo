# Contributing to Levo

Levo is a launchpad on Sequentia: a Python service that holds no keys, a
single-page app, and a command-line client. This page says how a change gets
in. What the pieces are and how to run them is in the [README](README.md);
the design notes are in [doc/](doc/).

## Run the gate before you open a pull request

```sh
npm --prefix web ci                 # once, and after the lockfile moves
python3 levod/tests/run.py          # unit checks, and the guards that walk the sources
python3 levod/tests/test_e2e.py     # the API end to end, over a stub node
npm --prefix web test && npm --prefix web run build
python3 levod/tests/test_render.py  # every page in a real browser; skipped without a chromium
```

Everything after `npm ci` runs on every push and pull request, and `main`
refuses a merge until it is green. Three more need a Sequentia node and a wallet, and say
so when they skip: `test_node.py`, `test_cli.py` and `test_browser.py`. Run
`test_node.py` whenever `covenant.py`, `tx.py`, `pset.py` or `watcher.py`
change; it is the only suite that proves anything about consensus.

## How a change lands

- One change per pull request, on a branch named `area/short-description`.
- Write the subject as `area: what changed`, one line, and put the reasoning in
  the body: why, rather than what. The diff already says what.
- Documentation is part of the change. If a README, a doc page or a comment
  becomes wrong, fix it in the same pull request, and write it in the present
  tense about current behaviour, with no history in it.
- Read the diff before you commit. The repository is public: no keys, seeds,
  wallet files, credentials or environment files, ever.

## Lines not to cross

- **levod holds no keys and signs nothing.** Do not add a route that accepts
  key material, a signing path, or a wallet call. That absence is why a
  compromised levod can mislead but cannot rob.
- **`levod/vectors.json` is frozen.** A failing covenant check means sale
  addresses have moved. Never regenerate the vectors to make it pass; that is a
  migration, and existing sales come first.
- **Refusals are sentences.** A person at the form typed into a box with a
  label; a refusal names that, not a JSON key.
- **An atom count is a decimal string on the wire.** In the app, every decision
  about an amount goes through `big()` or `positive()`; a test walks the sources
  for anything else.

## Reporting a security problem

Do not open a public issue for a vulnerability. Use the repository's private
vulnerability reporting on GitHub, which reaches the maintainers alone. GitHub's
dependency alerts are on for this repository as well, so a known problem in a
package the app or the gate pulls in reaches the maintainers without anyone
reporting it.
