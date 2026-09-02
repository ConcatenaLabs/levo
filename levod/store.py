"""Persistence: a single JSON file, written atomically.

Levo's durable state is small and entirely public -- project metadata, sale
terms, which staking keys an account proved, and how much each account has
committed. There are no keys and no balances here, because levod holds neither;
the tokens live in the covenant and the stake lives on chain.

It is still worth backing up. A funded sale's leaves are rebuilt from the
terms in this file, and a project that did not keep its own copy of the
listing response has no other way to derive the witness that spends its
covenant. Losing the file costs nobody tokens, but it can cost a project the
easy path to its reclaim, and it costs every buyer their allocation record.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Exit status for "the state on disk is not something levod may start from".
# It is a configuration failure, not a crash: restarting cannot fix it, and a
# supervisor that retried would bury the one message that says what to do.
# contrib/levod.service holds the matching RestartPreventExitStatus.
BAD_STATE_EXIT = 78


class Store:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("LEVOD_STATE", "levo-state.json"))
        self.data = {"projects": {}, "stake_links": {}, "version": 1}
        if self.path.is_file():
            self.load()
        self._sweep_temp()

    def _refuse(self, why):
        """Stop, with the reason where an operator will read it.

        The exit status says "do not restart me": a state file that cannot be
        read will not become readable in five seconds, and a restart loop turns
        one legible message into thousands of illegible ones.
        """
        sys.stderr.write("levod: %s\n" % why)
        sys.stderr.write("levod: not starting. Fix or restore the file, then "
                         "start levod again.\n")
        raise SystemExit(BAD_STATE_EXIT)

    def _sweep_temp(self):
        """Drop temp files a killed process left beside the state file. Each
        was an unfinished write; the state file itself was never touched."""
        try:
            for stale in self.path.parent.glob(".levo-*.tmp"):
                stale.unlink()
        except OSError:
            pass

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except ValueError as e:
            self._refuse("the state file %s is not valid JSON (%s). Restore it "
                         "from a backup rather than starting with an empty "
                         "ledger." % (self.path, e))
        except OSError as e:
            self._refuse("the state file %s cannot be read (%s)." % (self.path, e))
        if not isinstance(data, dict):
            self._refuse("the state file %s does not hold an object." % self.path)
        self.data = data
        self.data.setdefault("projects", {})
        self.data.setdefault("stake_links", {})

    def save(self):
        """Write via a temp file and rename, so an interrupted save cannot
        truncate the state that was already good."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".levo-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
