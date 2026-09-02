"""Persistence: what goes to disk comes back, including what the watcher
learns, and a broken file stops the service rather than emptying the ledger."""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import market as M  # noqa: E402
import sale as S  # noqa: E402
import store as ST  # noqa: E402
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
                         "treasury_prog": "11" * 32, "min_lot": 100, "close_locktime": 2_000_000_000,
                         "reclaim_xonly": "22" * 32, "total_atoms": 10_000})
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


def test_stale_temp_files_are_swept(t):
    d = Path(tempfile.mkdtemp())
    (d / ".levo-abc.tmp").write_text("{}")
    ST.Store(d / "state.json")
    t.ok(not (d / ".levo-abc.tmp").exists(), "a leftover temp file is removed at startup")


def test_saves_are_atomic(t):
    d = Path(tempfile.mkdtemp())
    s = ST.Store(d / "state.json")
    s.data["projects"] = {"x": 1}
    s.save()
    t.eq(json.loads((d / "state.json").read_text())["projects"], {"x": 1}, "written")
    t.eq(list(d.glob(".levo-*")), [], "and no temp file is left behind")
