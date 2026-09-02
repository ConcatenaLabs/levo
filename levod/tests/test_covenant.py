"""Covenant checks: the bytes, the refusals, and the arithmetic.

The sale covenant is the only part of Levo where a mistake costs money rather
than convenience, so these tests are about bytes and boundaries, not behaviour.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import covenant as C  # noqa: E402
import script as K  # noqa: E402

USDX = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"


def _terms(**kw):
    base = dict(token_asset="aa" * 32, payment_asset=USDX, price_num=25,
                price_den=100, treasury_prog="11" * 32, min_lot=100000,
                close_locktime=1_000_000, reclaim_xonly="22" * 32)
    base.update(kw)
    return C.SaleTerms(**base)


def test_golden_vectors(t):
    """Every frozen sale must still derive the same leaves and the same address.

    These vectors were produced by the covenant implementation already proven on
    the Sequentia testnet. A mismatch means sale addresses have MOVED.
    """
    vectors = json.loads((HERE.parent / "vectors.json").read_text())
    for v in vectors["cases"]:
        # The raw builders, which take WIRE-order asset ids. SaleTerms sits a
        # layer above and reverses display-form ids into these; that conversion
        # has its own test in test_tx.py.
        sell = C.build_sell_leaf(bytes.fromhex(v["token"]), bytes.fromhex(v["payment"]),
                                 v["rate_num"], v["rate_den"],
                                 bytes.fromhex(v["treasury_prog"]), v["min_lot"])
        reclaim = C.build_reclaim_leaf(v["close_locktime"], bytes.fromhex(v["reclaim_x"]))
        tap = K.Taptree(C.NUMS, [("sell", sell), ("reclaim", reclaim)])
        t.eq(sell.hex(), v["expect"]["sell_leaf"], "%s sell leaf" % v["name"])
        t.eq(reclaim.hex(), v["expect"]["reclaim_leaf"], "%s reclaim leaf" % v["name"])
        t.eq(tap.script_pubkey.hex(), v["expect"]["spk"], "%s address" % v["name"])


def test_address_is_a_function_of_the_terms(t):
    """Change any published term by the smallest amount and the address moves.

    This is what makes the published terms checkable: a buyer who rebuilds the
    address from them and gets a match knows the sale on chain is the sale that
    was advertised.
    """
    base = C.derive(_terms()).spk_hex
    for label, kw in [
        ("price up one atom", dict(price_num=26)),
        ("price denominator", dict(price_den=101)),
        ("different treasury", dict(treasury_prog="12" + "11" * 31)),
        ("different token", dict(token_asset="ab" + "aa" * 31)),
        ("different payment asset", dict(payment_asset="bb" * 32)),
        ("different minimum lot", dict(min_lot=100001)),
        ("different close", dict(close_locktime=1_000_001)),
        ("different reclaim key", dict(reclaim_xonly="23" + "22" * 31)),
    ]:
        t.ok(C.derive(_terms(**kw)).spk_hex != base, "address moves: %s" % label)


def test_nums_internal_key_is_mandatory(t):
    """A non-NUMS internal key is a hidden key path -- the project could spend
    the sale out from under its buyers. Levo must refuse to build one."""
    try:
        C.derive(_terms(), internal_key=bytes.fromhex("02" * 32)[:32])
        t.ok(False, "a non-NUMS internal key should be refused")
    except ValueError as e:
        t.ok("NUMS" in str(e), "refuses a non-NUMS internal key")
    t.eq(C.derive(_terms()).tap.internal_key, K.NUMS, "default internal key is NUMS")


def test_sell_witness_needs_no_signature(t):
    """The sell leaf is introspection-driven, so a buy carries no signature and
    no data -- which is exactly why the project need not be online."""
    w = C.derive(_terms()).sell_witness()
    t.eq(len(w), 2, "witness is leaf plus control block")
    t.eq(w[0], C.derive(_terms()).sell_leaf, "first item is the leaf script")
    t.eq(len(w[1]), 65, "control block is 65 bytes for a two-leaf tree")


def test_pricing_rounds_in_the_projects_favour(t):
    """The leaf computes ceil(n*num/den); Levo must quote the same number."""
    terms = _terms(price_num=7, price_den=3, min_lot=1)
    t.eq(terms.cost_for(1), 3, "ceil(7/3) is 3")
    t.eq(terms.cost_for(3), 7, "exact division is exact")
    t.eq(terms.cost_for(4), 10, "ceil(28/3) is 10")
    t.eq(_terms().cost_for(100_000_000), 25_000_000, "0.25 per token")


def test_minimum_lot_is_enforced_in_quotes(t):
    terms = _terms(min_lot=1000)
    try:
        terms.cost_for(999)
        t.ok(False, "a sub-minimum quote should be refused")
    except ValueError:
        t.ok(True, "refuses to quote below the minimum lot")


def test_overflow_bound_is_refused_at_listing(t):
    """The leaf's OP_MUL64 aborts on overflow, so an oversized sale would have
    tokens that can be reclaimed but never bought. Catch it while it is free."""
    try:
        _terms(price_num=10**12, total_atoms=10**13)
        t.ok(False, "an overflowing sale should be refused")
    except ValueError as e:
        t.ok("overflow" in str(e), "refuses a sale that overflows 64-bit pricing")
    _terms(price_num=25, price_den=100, total_atoms=10**15)   # comfortably fine
    t.ok(True, "accepts a realistically sized sale")


def test_terms_validation(t):
    for label, kw in [
        ("token priced in itself", dict(payment_asset="aa" * 32)),
        ("zero price", dict(price_num=0)),
        ("zero denominator", dict(price_den=0)),
        ("zero minimum lot", dict(min_lot=0)),
        ("no close", dict(close_locktime=0)),
    ]:
        try:
            _terms(**kw)
            t.ok(False, "should refuse: %s" % label)
        except ValueError:
            t.ok(True, "refuses %s" % label)


def test_verify_funding_catches_a_swapped_address(t):
    cov = C.derive(_terms())
    t.ok(cov.verify_funding(cov.spk_hex), "accepts the address it derives")
    other = C.derive(_terms(price_num=26)).spk_hex
    try:
        cov.verify_funding(other)
        t.ok(False, "should refuse an address from different terms")
    except ValueError:
        t.ok(True, "refuses an address that does not match the terms")


def test_json_round_trip(t):
    terms = _terms(total_atoms=10**12)
    again = C.SaleTerms.from_json(terms.to_json())
    t.eq(C.derive(again).spk_hex, C.derive(terms).spk_hex,
         "terms survive a JSON round trip byte-identically")


def test_canonical_price_preserves_cost_and_buys_headroom(t):
    """Reducing a price is a no-op on what anything costs, and the difference
    between a sale that works and one that overflows."""
    t.eq(C.canonical_price(25_000_000, 100_000_000), (1, 4), "25000000/100000000 is 1/4")
    t.eq(C.canonical_price(7, 3), (7, 3), "an already-reduced price is untouched")

    raw = _terms(price_num=25_000_000, price_den=100_000_000, min_lot=1)
    red = _terms(price_num=1, price_den=4, min_lot=1)
    for n in (1, 7, 12345, 10**8, 3 * 10**8 + 1):
        t.eq(raw.cost_for(n), red.cost_for(n), "same cost for %d atoms" % n)

    # The unreduced form cannot express a realistic sale; the reduced one can.
    try:
        raw.assert_no_overflow(10**14)
        t.ok(False, "the unreduced price should overflow on a 1M-token sale")
    except ValueError:
        t.ok(True, "the unreduced price overflows on a 1M-token sale")
    red.assert_no_overflow(10**14)
    t.ok(True, "the reduced price handles a 1M-token sale")

    # Reducing changes the address, which is why it must happen before funding.
    t.ok(C.derive(raw).spk_hex != C.derive(red).spk_hex,
         "the two forms derive different addresses despite quoting one price")


def test_the_generator_reproduces_the_frozen_vectors(t):
    """`tools/gen_vectors.py` must produce the file `covenant.py` checks itself
    against. A generator that drifts from the checker would, if ever run,
    write vectors the checker rejects and stop levod importing at all."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gen_vectors", HERE.parent.parent / "tools" / "gen_vectors.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    frozen = json.loads((HERE.parent / "vectors.json").read_text())
    t.eq(gen.build()["cases"], frozen["cases"], "the generator reproduces vectors.json")


def test_only_the_reclaim_leaf_carries_the_locktime(t):
    """The sell leaf has no locktime: the close opens the reclaim path and does
    not shut the sell path, and the copy says so. This pins the fact."""
    cov = C.derive(_terms())
    t.ok(bytes([K.OP_CHECKLOCKTIMEVERIFY]) not in cov.sell_leaf,
         "the sell leaf has no CHECKLOCKTIMEVERIFY")
    t.ok(bytes([K.OP_CHECKLOCKTIMEVERIFY]) in cov.reclaim_leaf,
         "the reclaim leaf has one")


def test_close_locktime_and_total_are_bounded(t):
    try:
        _terms(close_locktime=5_000_000_000)
        t.ok(False, "a close above 32 bits is refused")
    except ValueError as e:
        t.ok("4294967295" in str(e), "a close above 32 bits is refused, naming the bound")
    t.ok(_terms(close_locktime=0xffffffff) is not None, "the bound itself is allowed")
    try:
        _terms(total_atoms=99_999, min_lot=100_000)
        t.ok(False, "a total below the minimum lot is refused")
    except ValueError as e:
        t.ok("minimum lot" in str(e), "a total below the minimum lot is refused")
    for bad in (2.5, True, "abc"):
        try:
            _terms(min_lot=bad)
            t.ok(False, "min_lot %r refused" % (bad,))
        except ValueError:
            t.ok(True, "min_lot %r refused" % (bad,))
    t.eq(_terms(min_lot="100000").min_lot, 100000, "a decimal string is a whole number")
    try:
        C.SaleTerms.from_json({"token_asset": "aa" * 32})
        t.ok(False, "missing terms are named")
    except ValueError as e:
        t.ok("payment_asset" in str(e), "missing terms are named", str(e))
