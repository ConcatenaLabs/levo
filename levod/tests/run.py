#!/usr/bin/env python3
"""Run every Levo unit test. No dependencies, no framework, no network.

The two that need something outside this process live beside it and are run on
their own: `test_e2e.py` (the API end to end over a stub node), `test_node.py`
(a real sequentiad) and `test_render.py` (a real browser).
"""

import importlib
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

MODULES = ["test_crypto", "test_auth", "test_covenant", "test_address", "test_pset",
           "test_tiers", "test_rails", "test_rpc", "test_store", "test_tx", "test_watcher"]


class T:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def eq(self, got, want, what):
        self.ok(got == want, what, "got %r, want %r" % (got, want))

    def ok(self, cond, what, detail=""):
        if cond:
            self.passed += 1
        else:
            self.failed.append("%s%s" % (what, (" (%s)" % detail) if detail else ""))


def main():
    t = T()
    for name in MODULES:
        mod = importlib.import_module(name)
        for fn in sorted(x for x in dir(mod) if x.startswith("test_")):
            try:
                getattr(mod, fn)(t)
            except Exception:
                t.failed.append("%s.%s raised:\n%s" % (name, fn, traceback.format_exc()))
        print("  %-16s %d checks so far" % (name, t.passed))

    print()
    for f in t.failed:
        print("FAIL %s" % f)
    print("%d passed, %d failed" % (t.passed, len(t.failed)))
    return 1 if t.failed else 0


if __name__ == "__main__":
    sys.exit(main())
