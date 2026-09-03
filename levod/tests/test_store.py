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


def test_a_listing_that_contradicts_the_registry_is_refused(t):
    """Wallets read the registry. A sale that names its token otherwise would
    show one ticker here and another everywhere else, and the price a buyer
    typed would mean something different again."""
    import registry as REG

    class Entry:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self.body).encode()

    registered = {"contract": {"ticker": "REAL", "name": "The Real Token",
                               "precision": 2, "entity": {"domain": "example.test"}}}
    answer = REG.look_up("https://registry.test", "aa" * 32,
                         opener=lambda url, timeout=None: Entry(registered))
    t.eq(answer.ticker, "REAL", "the registry's own ticker is read")
    t.eq(REG.disagreement(answer, "REAL", 2), None, "a listing that agrees is fine")
    t.ok("not FAKE" in (REG.disagreement(answer, "FAKE", 2) or ""),
         "a different ticker is a disagreement")
    t.ok("2 places, not 8" in (REG.disagreement(answer, "REAL", 8) or ""),
         "and so is a different precision")

    def unreachable(url, timeout=None):
        raise OSError("no route to host")

    quiet = REG.look_up("https://registry.test", "aa" * 32, opener=unreachable)
    t.eq(quiet.checked, False, "a registry that cannot be reached checked nothing")
    t.eq(REG.disagreement(quiet, "ANYTHING", 8), None,
         "and blocks no listing, because a registry outage is not a project's fault")


def test_a_listing_made_before_the_registry_is_asked_about_later(t):
    """A sale listed before this Levo had a registry carries no answer, and its
    page says so. That is true but thin, and the answer costs one request."""
    d = Path(tempfile.mkdtemp())
    p = _platform(d / "state.json")
    p.registry_url = "https://registry.test"
    pr = p.list_project("02" + "11" * 32,
                        {"slug": "one", "name": "One", "ticker": "ONE", "decimals": 2},
                        {"token_asset": "aa" * 32, "payment_asset": USDX, "price_num": 1,
                         "price_den": 4, "treasury_prog": TREASURY_PROG, "min_lot": 100,
                         "close_locktime": 2_000_000_000, "reclaim_xonly": RECLAIM_XONLY,
                         "total_atoms": 10_000})
    pr.registry = {}                      # as a listing made before this existed
    import registry as REG

    class Entry:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"contract": {"ticker": "ONE", "name": "One Token",
                                            "precision": 2}}).encode()

    real = REG.urllib.request.urlopen
    REG.urllib.request.urlopen = lambda url, timeout=None: Entry()
    try:
        t.eq(p.check_registry(), ["one"], "the listing is asked about")
        t.eq(p.projects["one"].registry["ticker"], "ONE", "and the answer is kept")
        t.eq(p.check_registry(), [], "and not asked again")
    finally:
        REG.urllib.request.urlopen = real


def test_an_older_snapshot_cannot_overwrite_a_newer_one(t):
    """Snapshots are built under the platform's lock and written outside it, so
    two savers can build in one order and reach the disk in the other. The
    older one would then overwrite the newer, with nothing to show for it."""
    d = Path(tempfile.mkdtemp())
    st = ST.Store(d / "state.json")
    st.data = {"projects": {"a": 1}, "stake_links": {}}
    first, first_v = st.snapshot(), st.next_version()
    st.data = {"projects": {"a": 1, "b": 2}, "stake_links": {}}
    second, second_v = st.snapshot(), st.next_version()
    t.eq(st.write(second, version=second_v), True, "the newer write lands")
    t.eq(st.write(first, version=first_v), False, "the older one is dropped")
    back = json.loads((d / "state.json").read_text())
    t.eq(sorted(back["projects"]), ["a", "b"], "and the disk holds the newer state")
    t.eq(st.write(st.snapshot(), version=st.next_version()), True, "later writes still land")


def test_a_slower_save_never_overwrites_a_newer_one(t):
    """Two savers can build in one order and reach the disk in the other.

    The watcher saves with no platform lock held, so its snapshot can be built
    before a purchase and land after it. Without an order on the writes the
    purchase is on disk, then gone, with nothing dirty and nothing logged --
    and the next restart hands the buyer their whole tier cap back.
    """
    d = Path(tempfile.mkdtemp())
    st = ST.Store(d / "state.json")
    st.data = {"projects": {}, "note": "old"}
    old = st.snapshot()
    v_old = st.next_version()
    st.data = {"projects": {}, "note": "new"}
    new = st.snapshot()
    v_new = st.next_version()
    t.ok(st.write(new, version=v_new), "the newer write lands")
    t.ok(not st.write(old, version=v_old), "the older write is dropped")
    t.eq(json.loads((d / "state.json").read_text())["note"], "new",
         "what is on disk is the newer state")
    t.ok(not st.dirty, "a dropped write does not mark the file dirty")
    t.eq(st.write_error, None, "a dropped write is not an error")


def test_a_purchase_and_a_watcher_save_race_without_losing_the_purchase(t):
    """The real interleaving, with threads: a save loop beside a buyer."""
    import threading
    d = Path(tempfile.mkdtemp())
    p = _platform(d / "state.json")
    buyers = ["02%062x" % (0x22 + i) for i in range(40)]
    pr = p.list_project("02" + "11" * 32,
                        {"slug": "race", "name": "Race", "ticker": "RACE"},
                        {"token_asset": "aa" * 32, "payment_asset": USDX,
                         "price_num": 1, "price_den": 4,
                         "treasury_prog": TREASURY_PROG, "min_lot": 100,
                         "close_locktime": 2_000_000_000,
                         "reclaim_xonly": RECLAIM_XONLY, "total_atoms": 10_000})
    pr.sale.confirm_lock("ab" * 32, 1, pr.sale.script_pubkey, 10_000, "aa" * 32)
    stop = threading.Event()

    def saver():                      # what the watcher does, on its own poll
        while not stop.is_set():
            p.save()

    th = threading.Thread(target=saver, daemon=True)
    th.start()
    try:
        for i, buyer in enumerate(buyers):
            p.record_purchase(buyer, "race", "%064x" % (i + 1), 100, 25)
    finally:
        stop.set()
        th.join(timeout=5)
    p.save()
    on_disk = json.loads((d / "state.json").read_text())
    sale = on_disk["projects"]["race"]["sale"]
    # The ledger commits PAYMENT atoms, which is the unit a tier cap is in:
    # forty buys of 100 tokens at a quarter each.
    t.eq(sum(sale["allocations"].values()), 1000, "every purchase survived on disk")
    t.eq(sum(len(v) for v in sale["purchases"].values()), 40,
         "and so did every ledger entry")


def test_closing_soonest_compares_the_two_kinds_of_close(t):
    """A close is a height below 500,000,000 and a unix time above it.

    Sorted raw, every height close outranks every time close -- a sale closing
    at block 700,000 sits above one closing tomorrow. The order is over
    moments, so the two kinds can be compared with each other.
    """
    d = Path(tempfile.mkdtemp())
    p = _platform(d / "state.json")
    p.height = lambda strict=False: 200_000     # no node in this rig
    terms = {"token_asset": "aa" * 32, "payment_asset": USDX, "price_num": 1,
             "price_den": 4, "treasury_prog": TREASURY_PROG, "min_lot": 100,
             "reclaim_xonly": RECLAIM_XONLY, "total_atoms": 10_000}
    soon = int(time.time()) + 86_400            # tomorrow, as a time close
    for slug, close in (("far", 700_000), ("soon", soon)):  # listed far first
        pr = p.list_project("02" + "11" * 32,
                            {"slug": slug, "name": slug, "ticker": slug.upper()},
                            dict(terms, close_locktime=close, min_lot=100 + len(slug)))
        pr.sale.confirm_lock("%064x" % (hash(slug) & (2**256 - 1)), 0,
                             pr.sale.script_pubkey, 10_000, "aa" * 32)
    board = p.public_projects(sort="closing")
    t.eq([x["slug"] for x in board["projects"]], ["soon", "far"],
         "tomorrow's close sorts above one 347 days out")


def test_closing_soonest_survives_a_node_that_cannot_be_reached(t):
    """A height close needs the tip to become a moment. Without one it sorts
    last rather than taking the board down."""
    d = Path(tempfile.mkdtemp())
    p = _platform(d / "state.json")
    p.height = lambda strict=False: None
    pr = p.list_project("02" + "11" * 32,
                        {"slug": "height-close", "name": "H", "ticker": "HH"},
                        {"token_asset": "aa" * 32, "payment_asset": USDX,
                         "price_num": 1, "price_den": 4, "min_lot": 100,
                         "treasury_prog": TREASURY_PROG, "close_locktime": 700_000,
                         "reclaim_xonly": RECLAIM_XONLY, "total_atoms": 10_000})
    pr.sale.confirm_lock("ab" * 32, 0, pr.sale.script_pubkey, 10_000, "aa" * 32)
    board = p.public_projects(sort="closing")
    t.eq([x["slug"] for x in board["projects"]], ["height-close"],
         "the board still answers")


def test_a_damaged_funding_value_is_refused_at_load(t):
    """A file that parses as JSON can still hold a shape nothing can use.

    A string where the funding object belongs loaded cleanly and then broke
    every save, while the store went on reporting itself writable: the ledger
    stopped reaching the disk and nothing said so.
    """
    d = Path(tempfile.mkdtemp())
    path = d / "state.json"
    p = _platform(path)
    p.list_project("02" + "11" * 32, {"slug": "one", "name": "One", "ticker": "ONE"},
                   {"token_asset": "aa" * 32, "payment_asset": USDX, "price_num": 1,
                    "price_den": 4, "treasury_prog": TREASURY_PROG, "min_lot": 100,
                    "close_locktime": 2_000_000_000, "reclaim_xonly": RECLAIM_XONLY,
                    "total_atoms": 10_000})
    raw = json.loads(path.read_text())
    raw["projects"]["one"]["sale"]["funding"] = "f0f0:1"
    path.write_text(json.dumps(raw))
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            _platform(path)
        t.ok(False, "a damaged funding value stops the service")
    except SystemExit as e:
        t.eq(e.code, ST.BAD_STATE_EXIT, "a damaged funding value stops the service")
        t.ok("funding" in err.getvalue(), "and the message names what is wrong",
             err.getvalue()[:120])


def test_a_snapshot_that_cannot_be_built_shows_up_as_a_write_error(t):
    """Health reads `write_error` and `dirty` and nothing else, so a failure
    anywhere in the save has to reach them -- including one in the part that
    turns the platform into bytes."""
    d = Path(tempfile.mkdtemp())
    p = _platform(d / "state.json")
    p.projects["broken"] = object()          # a shape no snapshot can serialise
    try:
        p.save()
        t.ok(False, "a snapshot that cannot be built raises")
    except Exception:
        t.ok(True, "a snapshot that cannot be built raises")
    t.ok(p.store.dirty, "and the store knows the state file is behind")
    t.ok(p.store.write_error, "and health can say why")


def test_a_listing_cannot_carry_characters_that_are_not_text(t):
    """A project's words are printed on a page, in a terminal and in a log,
    and a control character means something different in each. A right-to-left
    override reverses a name against the amount beside it; a string of
    zero-width characters is a listing with no visible name at all."""
    d = Path(tempfile.mkdtemp())
    p = _platform(d / "state.json")
    terms = {"token_asset": "aa" * 32, "payment_asset": USDX, "price_num": 1,
             "price_den": 4, "treasury_prog": TREASURY_PROG, "min_lot": 100,
             "close_locktime": 2_000_000_000, "reclaim_xonly": RECLAIM_XONLY,
             "total_atoms": 10_000}
    bad = [("name", "Solar\u202egrid"),
           ("name", "\u200b\u200b\u200b"),
           ("name", "bell\x07here"),
           ("name", "two\nlines"),
           ("summary", "one\ttwo")]
    for i, (field, value) in enumerate(bad):
        meta = {"slug": "p%d" % i, "name": "Fine", "ticker": "FINE"}
        meta[field] = value
        try:
            p.list_project("02" + "11" * 32, meta, terms)
            t.ok(False, "%s %r is refused" % (field, value))
        except M.PlatformError:
            t.ok(True, "%s %r is refused" % (field, value))
    # An ordinary accented name is not caught up in it.
    pr = p.list_project("02" + "11" * 32,
                        {"slug": "eclair", "name": "\u00c9clair \u00c9nergie",
                         "ticker": "ECL"}, terms)
    t.eq(pr.name, "\u00c9clair \u00c9nergie", "an accented name is text like any other")


def test_a_link_is_one_address_and_says_where_it_goes(t):
    """Links are rendered as anchors on a public page: one that reads as one
    site and goes to another, or that carries a space or an invisible
    character, is refused rather than published."""
    for url in ("https://ok.test/\u202egnp.exe", "https://ok.test /",
                "https://name@evil.test/", "https://ok.test/\x00"):
        try:
            M.validate_links({"Site": url})
            t.ok(False, "%r is refused as a link" % url)
        except M.PlatformError:
            t.ok(True, "%r is refused as a link" % url)
    t.eq(M.validate_links({"Site": "https://ok.test/a?b=c#d"}),
         {"Site": "https://ok.test/a?b=c#d"}, "an ordinary URL is kept as it is")
