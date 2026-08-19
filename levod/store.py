"""Persistence: a single JSON file, written atomically.

Levo's durable state is small and entirely public -- project metadata, sale
terms, which staking keys an account proved, and how much each account has
committed. There are no keys and no balances here, because levod holds neither;
the tokens live in the covenant and the stake lives on chain. Losing this file
would cost the listings and the allocation ledger, not anybody's funds.
"""

import json
import os
import tempfile
from pathlib import Path


class Store:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("LEVOD_STATE", "levo-state.json"))
        self.data = {"projects": {}, "stake_links": {}, "version": 1}
        if self.path.is_file():
            self.load()

    def load(self):
        self.data = json.loads(self.path.read_text())
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
