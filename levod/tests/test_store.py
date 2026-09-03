"""Persistence: what goes to disk comes back, including what the watcher
learns, and a broken file stops the service rather than emptying the ledger."""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import market as M  # noqa: E402
import sale as S  # noqa: E402
import store as ST  # noqa: E402

# A reclaim key has to be a real x-only public key: Levo refuses a sale whose
# reclaim path could never be signed. This is the key of the secret 0x2222...22.
RECLAIM_XONLY = "466d7fcae563e5cb09a0d1870bb580344804617879a14949cf22285f1bae3f27"
# A taproot treasury is an output key, so it too has to be a point: the key of
# the secret 0x1111...11.
TREASURY_PROG = "4f355bdcb7cc0af728ef3cceb9615d90684bb5b2ca5f859ab0f0b704075871aa"
import tiers as T  # noqa: E402

USDX = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"


class Reader:
    def __init__(self):
        self.links = T.StakeLinks()
        self.policy = T.TierPolicy()

    def standing(self, a):
        return {"tier": {"may_list": True, "name": "Founder"}, "stake_atoms": 10**18, "stake": 1e10}


def _platform(path):
    return M.Platform(ST.Store(path), Reader(), None, None, hrp="tb")


def test_round_trip(t):
    d = Path(tempfile.mkdtemp())
    p = _platform(d / "state.json")
    pr = p.list_project("02" + "11" * 32, {"slug": "one", "name": "One", "ticker": "ONE",
                                         "links": {"Site": "https://example.test"}, "decimals": 2},
                        {"token_asset": "aa" * 32, "payment_asset": USDX, "price_num": 1, "price_den": 4,
                         "treasury_prog": TREASURY_PROG, "min_lot": 100, "close_locktime": 2_000_000_000,
                         "reclaim_xonly": RECLAIM_XONLY, "total_atoms": 10_000})
    sale = pr.sale
    sale.confirm_lock("ab" * 32, 1, sale.script_pubkey, 10_000, "aa" * 32)
    sale.funding["height"] = 95
    sale.funding["block"] = "block-95"
    sale.record_purchase("02" + "22" * 32, 25, 100, txid="cd" * 32, verified=True)
    sale.expect_remainder_at("cd" * 32)
    sale.note_reclaim("ef" * 32)
    sale.status = S.PARTIAL
    p.stake.links.link("02" + "11" * 32, "03" + "33" * 32)
    p.save()

    q = _platform(d / "state.json")
    got = q.projects["one"]
    t.eq(got.links, {"Site": "https://example.test"}, "links survive")
    t.eq(got.decimals, 2, "decimals survive")
    t.eq(got.sale.funding, {"txid": "ab" * 32, "vout": 1, "atoms": 10_000, "height": 95, "block": "block-95"},
         "the funding note, block included, survives")
    t.eq(got.sale.status, S.PARTIAL, "the raw status survives")
    t.eq(got.sale.allocations, {"02" + "22" * 32: 25}, "the ledger survives")
    t.eq(got.sale.purchases["02" + "22" * 32][0]["txid"], "cd" * 32, "purchases survive")
    t.eq(got.sale.candidates, [{"txid": "cd" * 32, "vout": 1}], "candidates survive")
    t.eq(got.sale.reclaim_txids, ["ef" * 32], "reclaims survive")
    t.eq(q.stake.links.keys_for("02" + "11" * 32), ["03" + "33" * 32], "stake links survive")
    t.eq(got.sale.token_label, "ONE", "and the sale knows its ticker for messages")
    t.eq(got.sale.token_decimals, 2, "and its decimals")


def test_a_corrupt_state_file_stops_the_service(t):
    """And stops it in a way a supervisor will not fight: the reason is on
    stderr and the exit status says restarting cannot help, so levod stays
    down with one legible message instead of looping every five seconds."""
    d = Path(tempfile.mkdtemp())
    (d / "state.json").write_text("{not json")
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            ST.Store(d / "state.json")
        t.ok(False, "a corrupt file is refused")
    except SystemExit as e:
        t.eq(e.code, ST.BAD_STATE_EXIT, "with the do-not-restart status")
        t.ok("not valid JSON" in err.getvalue(), "and a reason on stderr")
        t.ok("backup" in err.getvalue(), "that says what to do about it")


def test_stale_temp_files_are_swept_and_live_ones_are_not(t):
    """A temp file is what a save writes through. Sweeping every one of them at
    startup means a second levod -- a hand-run copy against the same file, which
    builds its Store long before it fails to bind a port -- deletes the file the
    running one is writing, and that save fails."""
    d = Path(tempfile.mkdtemp())
    old, live = d / ".levo-old.tmp", d / ".levo-live.tmp"
    old.write_text("{}")
    live.write_text("{}")
    os.utime(old, (0, time.time() - ST.STALE_TEMP_SECONDS - 60))
    ST.Store(d / "state.json")
    t.ok(not old.exists(), "a temp file left by a dead process is removed")
    t.ok(live.exists(), "one a running process may be writing is left alone")


def test_a_failed_write_is_remembered(t):
    """The change is already in memory when the write fails, so a levod that
    carries on is serving a ledger that exists nowhere else."""
    d = Path(tempfile.mkdtemp())
    st = ST.Store(d / "state.json")
    st.save()
    t.eq(st.write_error, None, "a good save leaves nothing behind")
    st.path = Path("/proc/levo-cannot-write/state.json")
    try:
        st.save()
        t.ok(False, "a failed write raises")
    except Exception:
        t.ok(st.write_error is not None, "and is remembered")
        t.eq(st.dirty, True, "with the state marked as ahead of the disk")


def test_saves_are_atomic(t):
    d = Path(tempfile.mkdtemp())
    s = ST.Store(d / "state.json")
    s.data["projects"] = {"x": 1}
    s.save()
    t.eq(json.loads((d / "state.json").read_text())["projects"], {"x": 1}, "written")
    t.eq(list(d.glob(".levo-*")), [], "and no temp file is left behind")


def test_damage_inside_the_state_file_stops_the_service_too(t):
    """A file can parse as JSON and still be unreadable: a listing without its
    terms, a truncated write from a full disk. Raising through would give a
    supervisor a crash loop and the operator no message."""
    d = Path(tempfile.mkdtemp())
    (d / "state.json").write_text(json.dumps({"projects": {"broken": {"slug": "broken"}}}))
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            _platform(d / "state.json")
        t.ok(False, "a damaged listing is refused")
    except SystemExit as e:
        t.eq(e.code, ST.BAD_STATE_EXIT, "with the do-not-restart status")
        t.ok("cannot be read" in err.getvalue(), "and a reason on stderr")


def test_every_route_is_documented(t):
    """Documentation ships with the change. A route nobody wrote down is one an
    integrator has to read Python to find, and then depends on whatever they
    inferred.

    The routes come from the server's own table rather than from a regex over
    its source, and each one has to appear in the reference as itself: matching
    on the last word of a path passed for almost anything, which is how this
    check quietly stopped checking.
    """
    import server as SRV
    root = HERE.parent.parent
    doc = (root / "doc" / "api.md").read_text()
    missing = []
    for shape, _methods in SRV.API_METHODS:
        route = "/api/" + "/".join("<slug>" if part == "*" else part for part in shape)
        if route not in doc:
            missing.append(route)
    t.eq(sorted(missing), [], "every route levod serves is in doc/api.md")


def test_a_sale_that_no_longer_derives_its_own_address_stops_the_service(t):
    """Everything about a sale rests on its address: the watcher reads it,
    buyers pay it, the reclaim spends it. Terms that derive a different one
    than they did when the sale was funded mean levod would watch, quote and
    reclaim the wrong address, quietly."""
    d = Path(tempfile.mkdtemp())
    p = _platform(d / "state.json")
    p.list_project("02" + "11" * 32,
                   {"slug": "one", "name": "One", "ticker": "ONE", "decimals": 2},
                   {"token_asset": "aa" * 32, "payment_asset": USDX, "price_num": 1,
                    "price_den": 4, "treasury_prog": TREASURY_PROG, "min_lot": 100,
                    "close_locktime": 2_000_000_000, "reclaim_xonly": RECLAIM_XONLY,
                    "total_atoms": 10_000})
    p.save()
    raw = json.loads((d / "state.json").read_text())
    t.ok(raw["projects"]["one"]["sale"]["script_pubkey"], "the address is written down")
    raw["projects"]["one"]["sale"]["terms"]["min_lot"] = 101      # a different sale
    (d / "state.json").write_text(json.dumps(raw))
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            _platform(d / "state.json")
        t.ok(False, "the mismatch is refused")
    except SystemExit as e:
        t.eq(e.code, ST.BAD_STATE_EXIT, "with the do-not-restart status")
        t.ok("no longer derives" in err.getvalue(), "and says what happened")


def test_the_ledger_is_indexed_by_transaction(t):
    """A purchase counts once, and finding out whether one is already recorded
    runs under the platform lock on the path a busy sale takes most often. It
    is a lookup, not a walk over every entry of every account."""
    d = Path(tempfile.mkdtemp())
    p = _platform(d / "state.json")
    pr = p.list_project("02" + "11" * 32,
                        {"slug": "one", "name": "One", "ticker": "ONE", "decimals": 2},
                        {"token_asset": "aa" * 32, "payment_asset": USDX, "price_num": 1,
                         "price_den": 4, "treasury_prog": TREASURY_PROG, "min_lot": 100,
                         "close_locktime": 2_000_000_000, "reclaim_xonly": RECLAIM_XONLY,
                         "total_atoms": 10_000})
    sale = pr.sale
    sale.record_purchase("02" + "22" * 32, 25, 100, txid="cd" * 32)
    t.eq(sale.recorded_by("cd" * 32), "02" + "22" * 32, "the index knows who recorded it")
    t.eq(sale.recorded_by("ff" * 32), None, "and knows when nobody did")
    p.save()
    q = _platform(d / "state.json")
    t.eq(q.projects["one"].sale.recorded_by("cd" * 32), "02" + "22" * 32,
         "and it is rebuilt when the state is read back")
