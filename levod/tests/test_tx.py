"""Transaction checks: byte order, output layout, and balance.

The output layout tests are the ones that matter most. A buy transaction whose
outputs sit in the wrong order is not rejected for being untidy -- it is either
rejected by the covenant, or accepted while meaning something other than what
the buyer intended.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import covenant as C  # noqa: E402
import sale as S  # noqa: E402
import tiers as T  # noqa: E402
import tx as TX  # noqa: E402

USDX = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"
GOLD = "3a0f9192219db59f8d7f87d93ac6311095dfe1255d149727b87baaa7d2cc71a1"
TOP = 25 * T.POS_MIN_STAKE_ATOMS


def _sale(total=100_000 * 10**8, min_lot=50 * 10**8, price=(1, 4)):
    terms = C.SaleTerms(GOLD, USDX, price[0], price[1], "11" * 32, min_lot,
                        2_000_000_000, "22" * 32, total)
    s = S.Sale("t", terms, "issuer")
    s.confirm_lock("ab" * 32, 0, s.script_pubkey, total, GOLD)
    return s


def _buyer(**kw):
    b = {
        "token_script_pubkey": TX.v1_script_pubkey("aa" * 32).hex(),
        "change_script_pubkey": TX.v1_script_pubkey("bb" * 32).hex(),
        "inputs": [{"txid": "cd" * 32, "vout": 1, "asset": USDX,
                    "value_atoms": 100_000 * 10**8}],
        "fee_atoms": 1000, "fee_asset": USDX,
    }
    b.update(kw)
    return b


def test_asset_ids_are_reversed_onto_the_wire(t):
    """Asset ids are displayed in the reverse of their wire order. Writing the
    display form straight onto the wire builds a sale priced in a different
    asset -- one nobody holds, which no buyer could ever fill.

    Verified against a live Sequentia node: supplying 2a5155...b9de unreversed
    made `decoderawtransaction` report the asset as deb904...512a.
    """
    t.eq(C.asset_to_wire(USDX).hex(),
         "deb9044d8fa54b848820dd7d71150dc1c0ba65cd6e76a7ca606a5eda3955512a",
         "USDX display form reverses to its wire form")
    t.eq(C.asset_to_wire(C.asset_to_wire(USDX).hex()).hex(), USDX,
         "reversing twice returns the display form")
    t.eq(TX.explicit_asset(USDX), b"\x01" + C.asset_to_wire(USDX),
         "an output commits to the wire form")
    # And the covenant leaf must commit to the same bytes the chain will push.
    t.ok(C.asset_to_wire(USDX) in C.derive(_sale().terms).sell_leaf,
         "the sell leaf carries the payment asset in wire order")
    t.ok(C.asset_to_wire(GOLD) in C.derive(_sale().terms).sell_leaf,
         "the sell leaf carries the token asset in wire order")


def test_explicit_value_is_big_endian(t):
    """Unlike almost everything else in the format. Backwards here serialises
    cleanly and is worth a wildly different amount."""
    t.eq(TX.explicit_value(1), b"\x01" + (1).to_bytes(8, "big"), "one atom")
    t.eq(TX.explicit_value(250 * 10**8).hex(),
         "01" + (250 * 10**8).to_bytes(8, "big").hex(), "250 units")
    t.eq(TX.explicit_value(250 * 10**8).hex(), "0100000005d21dba00",
         "250 units, pinned against a live node's decode")


def test_txids_are_reversed_onto_the_wire(t):
    i = TX.TxIn("0123456789abcdef" * 4, 3)
    t.eq(i.serialize()[:32].hex(),
         bytes.fromhex("0123456789abcdef" * 4)[::-1].hex(),
         "the outpoint hash goes on the wire reversed")
    t.eq(i.serialize()[32:36].hex(), "03000000", "the index is little-endian")


def test_partial_buy_layout(t):
    s = _sale()
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP), token_atoms=1000 * 10**8, height=1)
    built = TX.build_buy(s, plan, _buyer())
    outs = built["outputs"]
    t.eq(outs[0]["asset"], USDX, "output 0 is the treasury credit")
    t.eq(outs[0]["atoms"], 250 * 10**8, "and it is the ceiling price")
    t.eq(outs[0]["script_pubkey"], TX.v1_script_pubkey("11" * 32).hex(),
         "paid to the published treasury")
    t.eq(outs[1]["asset"], GOLD, "output 1 is the remainder")
    t.eq(outs[1]["script_pubkey"], s.cov.script_pubkey.hex(),
         "re-rested at the identical covenant address")
    t.eq(outs[1]["atoms"], 99_000 * 10**8, "remainder is what did not sell")
    t.eq(outs[2]["asset"], GOLD, "the buyer's tokens come after the covenant's slots")
    t.eq(outs[2]["atoms"], 1000 * 10**8, "and are what was bought")
    t.eq(outs[-1]["script_pubkey"], "", "the fee output has no scriptPubKey")


def test_full_buy_never_puts_the_token_at_output_one(t):
    """On a full buy the covenant reads output 2k+1 to decide whether a
    remainder exists. The buyer's own tokens sitting there would be read as an
    unsold remainder and required to be at the covenant address."""
    s = _sale(total=1000 * 10**8)
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP),
                      token_atoms=s.locked_atoms, height=1)
    built = TX.build_buy(s, plan, _buyer())
    t.eq(plan.remainder_atoms, 0, "this is a full buy")
    t.ok(built["outputs"][1]["asset"] != GOLD,
         "output 1 on a full buy is not the sale token")
    tokens = [o for o in built["outputs"] if o["asset"] == GOLD]
    t.eq(len(tokens), 1, "exactly one token output")
    t.eq(tokens[0]["atoms"], 1000 * 10**8, "the buyer gets all of them")
    t.ok(tokens[0]["index"] >= 2, "and it sits where the covenant does not look")


def test_full_buy_with_no_change_and_no_fee_is_refused(t):
    """Rather than silently producing a transaction the covenant will reject."""
    s = _sale(total=1000 * 10**8)
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP),
                      token_atoms=s.locked_atoms, height=1)
    try:
        TX.build_buy(s, plan, _buyer(
            inputs=[{"txid": "cd" * 32, "vout": 1, "asset": USDX,
                     "value_atoms": 250 * 10**8}],
            fee_atoms=0, change_script_pubkey=None))
        t.ok(False, "should refuse a full buy with nothing to put at output 1")
    except TX.BuildError as e:
        t.ok("output 1" in str(e) or "second output" in str(e),
             "explains why a second output is needed")


def test_covenant_input_is_first_and_unsigned(t):
    s = _sale()
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP), token_atoms=1000 * 10**8, height=1)
    built = TX.build_buy(s, plan, _buyer())
    t.eq(built["inputs"][0]["role"], "the sale covenant", "the covenant is input 0")
    t.ok("none" in built["inputs"][0]["signing"],
         "and it needs no signature at all")
    t.ok("sign" in built["inputs"][1]["signing"], "the buyer signs their own input")


def test_underfunded_and_stray_assets_are_refused(t):
    s = _sale()
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP), token_atoms=1000 * 10**8, height=1)
    try:
        TX.build_buy(s, plan, _buyer(inputs=[{"txid": "cd" * 32, "vout": 1,
                                              "asset": USDX, "value_atoms": 10}]))
        t.ok(False, "should refuse an underfunded purchase")
    except TX.BuildError:
        t.ok(True, "refuses an underfunded purchase")
    try:
        TX.build_buy(s, plan, _buyer(inputs=[
            {"txid": "cd" * 32, "vout": 1, "asset": USDX, "value_atoms": 100_000 * 10**8},
            {"txid": "ce" * 32, "vout": 0, "asset": GOLD, "value_atoms": 5}]))
        t.ok(False, "should refuse an input that would be burned")
    except TX.BuildError as e:
        t.ok("burned" in str(e), "refuses an input it would have to burn")


def test_change_without_an_address_is_refused(t):
    """Rather than quietly handing the difference to the network as fee."""
    s = _sale()
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP), token_atoms=1000 * 10**8, height=1)
    try:
        TX.build_buy(s, plan, _buyer(change_script_pubkey=None))
        t.ok(False, "should refuse to drop change")
    except TX.BuildError as e:
        t.ok("change" in str(e), "refuses to burn change")


def test_transaction_balances(t):
    """Every asset in equals every asset out. Elements enforces this too, but
    only after the buyer has signed and broadcast."""
    s = _sale()
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP), token_atoms=1000 * 10**8, height=1)
    built = TX.build_buy(s, plan, _buyer())
    ins = {USDX: 100_000 * 10**8, GOLD: s.locked_atoms}
    outs = {}
    for o in built["outputs"]:
        outs[o["asset"]] = outs.get(o["asset"], 0) + o["atoms"]
    t.eq(outs, ins, "inputs and outputs balance on every asset")


def test_txid_matches_a_real_node(t):
    """Pinned from `decoderawtransaction` on a live Sequentia node (node000,
    chain test). If serialisation drifts, this is what catches it."""
    s = C.SaleTerms(GOLD, USDX, 1, 4, "11" * 32, 50 * 10**8, 2_000_000_000,
                    "22" * 32, 100_000 * 10**8)
    sale = S.Sale("t", s, "issuer")
    sale.confirm_lock("0123456789abcdef" * 4, 3, sale.script_pubkey,
                      100_000 * 10**8, GOLD)
    plan = sale.plan_buy("b", T.TierPolicy().for_stake(TOP),
                         token_atoms=1000 * 10**8, height=1)
    built = TX.build_buy(sale, plan, _buyer(
        inputs=[{"txid": "fedcba9876543210" * 4, "vout": 1, "asset": USDX,
                 "value_atoms": 500 * 10**8}]))
    t.eq(built["txid"],
         "ac71f527bc41bd2bc90395a5ce30bd6b9e08436ea8b8e05d342ab4592ae96c1c",
         "txid matches what a live node computed for these exact bytes")


def test_set_witness_replaces_one_input_and_leaves_the_rest(t):
    """A wallet signs the inputs it owns and leaves the covenant's alone,
    because it knows nothing about the leaf. The covenant's witness goes in
    afterwards, and putting it in the wrong place would either strip the leaf or
    corrupt the transaction. Verified against a live node's decode."""
    s = _sale()
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP), token_atoms=1000 * 10**8, height=1)
    built = TX.build_buy(s, plan, _buyer())
    marker = [b"\xde" * 64, b"\xad" * 33]
    out = TX.set_witness(built["unsigned_tx_hex"], 1, marker)

    t.ok(out != built["unsigned_tx_hex"], "the transaction changed")
    t.ok(marker[0].hex() in out, "the new witness is present")
    t.ok(s.cov.sell_leaf.hex() in out, "the covenant's leaf survived untouched")
    # And the covenant's own witness can be replaced without disturbing others.
    out2 = TX.set_witness(out, 0, [b"\x01", b"\x02"])
    t.ok(marker[0].hex() in out2, "input 1's witness is still there")

    try:
        TX.set_witness(built["unsigned_tx_hex"], 9, marker)
        t.ok(False, "should refuse an input index that does not exist")
    except TX.BuildError:
        t.ok(True, "refuses an input index that does not exist")
