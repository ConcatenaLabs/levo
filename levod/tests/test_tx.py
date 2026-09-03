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


def test_reclaim_builds_what_the_project_signs(t):
    """A reclaim hands back the sighash, the leaf and the control block, and
    never a signature: the key is the project's. The witness assembled from a
    signature over that sighash is what the CLI puts on the wire."""
    import secp256k1 as K
    sec = 0x2222222222222222222222222222222222222222222222222222222222222222
    xonly = K.xonly_pubkey(sec).hex()
    terms = C.SaleTerms(GOLD, USDX, 1, 4, "11" * 32, 50 * 10**8, 100, xonly, 100_000 * 10**8)
    s = S.Sale("t", terms, "issuer")
    s.confirm_lock("ab" * 32, 0, s.script_pubkey, 100_000 * 10**8, GOLD)
    fee_inputs = [{"txid": "cd" * 32, "vout": 1, "asset": USDX, "value_atoms": 5000,
                   "script_pubkey": "0014" + "cc" * 20}]
    try:
        TX.build_reclaim(s, "5120" + "dd" * 32, fee_inputs, 1000, USDX, "ee" * 32)
        t.ok(False, "fee-input change with nowhere to go is refused")
    except TX.BuildError:
        t.ok(True, "fee-input change with nowhere to go is refused")
    r = TX.build_reclaim(s, "5120" + "dd" * 32, fee_inputs, 1000, USDX, "ee" * 32,
                         change_spk="0014" + "ee" * 20)
    t.ok("signature" not in r, "levod signs nothing")
    # The tokens go where the project said; the leftover of its fee input goes
    # to its own change address, not along with them.
    raw = r["unsigned_tx_hex"]
    t.ok(("5120" + "dd" * 32) in raw, "the tokens go to the destination")
    t.ok(("0014" + "ee" * 20) in raw, "the change goes to the change address")
    t.eq(raw.count("5120" + "dd" * 32), 1,
         "and the destination is paid exactly once")
    t.eq(r["signs_with"], xonly, "it names the key that must sign")
    t.eq(r["locktime"], 100, "the locktime is the close")
    t.eq(len(r["sighash"]), 64, "a 32-byte sighash")
    sig = K.schnorr_sign(bytes.fromhex(r["sighash"]), sec)
    t.ok(TX.check_reclaim_signature(s, r["sighash"], sig), "a signature by the reclaim key checks")
    t.ok(not TX.check_reclaim_signature(s, r["sighash"], K.schnorr_sign(bytes.fromhex(r["sighash"]), sec + 1)),
         "one by another key does not")
    stack = TX.reclaim_witness(s, sig)
    t.eq([len(x) for x in stack][0], 64, "the witness starts with the signature")
    t.eq(stack[1], s.cov.reclaim_leaf, "then the leaf")
    finished = TX.set_witness(r["unsigned_tx_hex"], 0, stack)
    t.ok(len(finished) > len(r["unsigned_tx_hex"]), "and the witness fits into the transaction")
    try:
        TX.build_reclaim(s, "5120" + "dd" * 32, fee_inputs, 1000, USDX, "ee" * 32,
                         locktime=99, change_spk="0014" + "ee" * 20)
        t.ok(False, "a locktime before the close is refused")
    except TX.BuildError:
        t.ok(True, "a locktime before the close is refused")
    s.locked_atoms = 0
    try:
        TX.build_reclaim(s, "5120" + "dd" * 32, fee_inputs, 1000, USDX, "ee" * 32,
                         change_spk="0014" + "ee" * 20)
        t.ok(False, "an empty covenant has nothing to reclaim")
    except TX.BuildError:
        t.ok(True, "an empty covenant has nothing to reclaim")


def test_a_fee_in_the_sale_token_never_lands_at_output_one_on_a_full_buy(t):
    """The sell leaf reads any token-asset output at index 1 as a remainder that
    must rest at the covenant. Paying the fee in the token would put one
    there; the builder refuses unless payment-asset change can take the slot."""
    s = _sale(total=100 * 10**8, min_lot=100 * 10**8)
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP), token_atoms=100 * 10**8)
    tok_in = {"txid": "ef" * 32, "vout": 0, "asset": GOLD, "value_atoms": 5000,
              "script_pubkey": "0014" + "cc" * 20}
    pay_in = {"txid": "cd" * 32, "vout": 1, "asset": USDX, "value_atoms": 25 * 10**8,
              "script_pubkey": "0014" + "cc" * 20}
    try:
        TX.build_buy(s, plan, _buyer(inputs=[pay_in, tok_in], fee_asset=GOLD, fee_atoms=5000))
        t.ok(False, "a token fee with no other change is refused")
    except TX.BuildError as e:
        t.ok("sale token" in str(e), "a token fee with no other change is refused with the reason")
    built = TX.build_buy(s, plan, _buyer(inputs=[dict(pay_in, value_atoms=30 * 10**8), tok_in],
                                         fee_asset=GOLD, fee_atoms=4000))
    t.eq(built["outputs"][1]["asset"], USDX, "payment change takes output 1")
    roles = [o["role"] for o in built["outputs"]]
    t.ok("your tokens" in roles and roles.count("your tokens") == 1, "exactly one output is the buyer's tokens")
    t.ok("your change" in roles, "token change is labelled as change")
    t.ok(built["pset"], "a PSET comes with it")


def test_the_pset_matches_the_transaction(t):
    import pset as P
    s = _sale()
    plan = s.plan_buy("b", T.TierPolicy().for_stake(TOP), token_atoms=1000 * 10**8)
    built = TX.build_buy(s, plan, _buyer(inputs=[{"txid": "cd" * 32, "vout": 1, "asset": USDX,
                                                  "value_atoms": 1000 * 10**8,
                                                  "script_pubkey": "0014" + "cc" * 20}]))
    maps = P.parse_maps(built["pset"])
    g, i0, i1 = maps[0], maps[1], maps[2]
    t.eq(len(maps), 1 + 2 + len(built["outputs"]), "one map per input and output")
    t.ok(any(k == b"\x08" for k, _ in i0), "the covenant input carries its final witness")
    t.ok(not any(k == b"\x08" for k, _ in i1), "the buyer's input is left to the wallet")
    t.ok(any(k == b"\x01" for k, _ in i1), "and carries the output it spends")
    built2 = TX.build_buy(s, plan, _buyer(inputs=[{"txid": "cd" * 32, "vout": 1, "asset": USDX,
                                                   "value_atoms": 1000 * 10**8}]))
    t.eq(built2["pset"], None, "without the spent script there is no PSET, and the hex still stands")


def test_reclaim_locktime_kinds_must_match_the_close(t):
    import secp256k1 as K
    xonly = K.xonly_pubkey(5).hex()
    terms = C.SaleTerms(GOLD, USDX, 1, 4, "11" * 32, 50 * 10**8, 100, xonly, 100_000 * 10**8)
    s = S.Sale("t", terms, "issuer")
    s.confirm_lock("ab" * 32, 0, s.script_pubkey, 100_000 * 10**8, GOLD)
    fee_inputs = [{"txid": "cd" * 32, "vout": 1, "asset": USDX, "value_atoms": 5000,
                   "script_pubkey": "0014" + "cc" * 20}]
    try:
        TX.build_reclaim(s, "5120" + "dd" * 32, fee_inputs, 1000, USDX, "ee" * 32, locktime=1_700_000_000)
        t.ok(False, "a time locktime on a height-closed sale is refused")
    except TX.BuildError as e:
        t.ok("block height" in str(e), "a time locktime on a height-closed sale is refused", str(e))
    try:
        TX.build_reclaim(s, "", fee_inputs, 1000, USDX, "ee" * 32)
        t.ok(False, "an empty destination is refused")
    except TX.BuildError:
        t.ok(True, "an empty destination is refused, since it would be a fee output")


def test_set_witness_walks_confidential_outputs(t):
    """A wallet may add a blinded change output. The walker has to step over
    33-byte commitments and 1-byte nulls, or the witness lands mid-proof."""
    tx = TX.Transaction()
    tx.vin.append(TX.TxIn("ab" * 32, 0))
    tx.vin.append(TX.TxIn("cd" * 32, 1))
    tx.vout.append(TX.TxOut(USDX, 5, "5120" + "11" * 32))
    raw = bytearray(tx.serialize(with_witness=False))
    # Splice a confidential output in by hand: asset commitment, value
    # commitment, nonce commitment, empty script.
    conf = (b"\x0a" + b"\x11" * 32 + b"\x08" + b"\x22" * 32 + b"\x02" + b"\x33" * 32 + b"\x00")
    n_out_pos = 4 + 1 + 1 + 2 * (32 + 4 + 1 + 4)
    raw[n_out_pos] = 2
    body = bytes(raw[:-4]) + conf + bytes(raw[-4:])
    stack = [b"\x01", b"\x51"]
    out = TX.set_witness(body.hex(), 1, stack)
    t.ok(out.endswith((b"\x00\x00" + b"\x02\x01\x01\x01\x51" + b"\x00" + b"\x00\x00" * 2).hex()),
         "the witness for input 1 is written after input 0's empty witness")
    # And back through a witnessed transaction: replacing again keeps the layout.
    again = TX.set_witness(out, 0, [b"\x02"])
    t.ok(again.endswith((b"\x00\x00" + b"\x02\x01\x01\x01\x51" + b"\x00" + b"\x00\x00" * 2).hex()),
         "input 1's witness survives a later write to input 0")
