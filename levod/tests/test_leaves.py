"""Buying from leaves: a round that fills a sale for many buyers, attributed.

The operator's round spends the sale once for every take of it, at any input
index k, credits the treasury at 2k and rests the remainder at 2k+1. Which
buyer took which share comes from the operator; whether the round is in the
chain, spends this sale and fills at least those shares comes from the node.
A take counts once its round is in a block, and stops counting when that
block leaves the chain.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import covenant as C  # noqa: E402
import leaves as LV  # noqa: E402
import sale as S  # noqa: E402
import script as K  # noqa: E402
import watcher as W  # noqa: E402
from test_watcher import FakeMarket, FakeRPC, P, USDX, GOLD, TOTAL  # noqa: E402

FUND = "ab" * 32
ROUND = "cd" * 32


def _sale():
    terms = C.SaleTerms(GOLD, USDX, 3, 2, "11" * 32, 10_000, 2_000_000_000, "22" * 32, TOTAL)
    s = S.Sale("t", terms, "issuer")
    s.confirm_lock(FUND, 0, s.script_pubkey, TOTAL, GOLD)
    s.funding["seen_height"] = 100
    return s


def _status(s, take, filled, state="issued", rnd=None, k=None, round_filled=None, order=(FUND, 0), locked=TOTAL):
    """A take's status as the operator serves it."""
    st = {"participation_id": take, "state": state, "fill": {
        "order": "%s:%d" % order, "asset_a": s.terms.token_asset, "asset_b": s.terms.payment_asset,
        "locked": str(locked), "filled": str(filled), "remainder": str(locked - filled),
        "price": str(s.terms.cost_for(filled)), "maker_script": s.terms.treasury_spk.hex(), "script": s.script_pubkey}}
    if rnd:
        st["round"] = {"txid": rnd, "connector_vout": 9}
        st["fill"].update({"input": k, "credit_vout": 2 * k, "remainder_vout": 2 * k + 1})
        if round_filled is not None:
            st["fill"]["round_filled"] = str(round_filled)
    return st


def _round(s, k, filled, locked=TOTAL, credit=None, order=(FUND, 0)):
    """The round's transaction, decoded as a node gives it: another order at
    inputs before k, the sale at k, the treasury at 2k and the rest at 2k+1."""
    vin = [{"txid": "ee" * 32, "vout": i} for i in range(k)] + [{"txid": order[0], "vout": order[1]}]
    vout = []
    for i in range(k):
        vout += [{"n": 2 * i, "value": 0.001, "asset": USDX, "scriptPubKey": {"hex": "5120" + "33" * 32}},
                 {"n": 2 * i + 1, "value": 0.00000001, "asset": USDX, "scriptPubKey": {"hex": "0014" + "44" * 20}}]
    credit = s.terms.cost_for(filled) if credit is None else credit
    vout.append({"n": 2 * k, "valueatoms": credit, "asset": USDX, "scriptPubKey": {"hex": s.terms.treasury_spk.hex()}})
    vout.append({"n": 2 * k + 1, "valueatoms": locked - filled, "asset": GOLD, "scriptPubKey": {"hex": s.script_pubkey}})
    vout.append({"n": 2 * k + 2, "valueatoms": filled, "asset": GOLD, "scriptPubKey": {"hex": "5120" + "55" * 32}})
    return {"txid": ROUND, "vin": vin, "vout": vout}


class Operator:
    def __init__(self):
        self.statuses = {}

    def take(self, take_id):
        if take_id not in self.statuses:
            raise LV.LeafError("the operator knows no take %s" % take_id)
        return self.statuses[take_id]


class Node(FakeRPC):
    def __init__(self):
        super().__init__()
        self.headers = {}

    def call(self, method, *params):
        if method == "getblockhash" and int(params[0]) > self.height:
            raise RuntimeError("Block height out of range")
        if method == "getblockheader":
            return {"height": self.headers[params[0]]}
        if method == "getrawtransaction":
            tx = self.txs.get(params[0])
            if tx is None:
                raise RuntimeError("No such mempool or blockchain transaction")
            return tx
        return super().call(method, *params)


def test_the_sale_as_an_order(t):
    s = _sale()
    o = LV.order_form(s)
    t.eq((o["covenant_txid"], o["covenant_vout"]), (FUND, 0), "the order rests where the sale does")
    t.eq(o["asset_a"], bytes.fromhex(GOLD)[::-1].hex(), "asset ids in wire order")
    t.eq(o["asset_b"], bytes.fromhex(USDX)[::-1].hex(), "the payment asset too")
    t.eq((o["rate_num"], o["rate_den"], o["min_lot"], o["maker_prog_ver"]), ("3", "2", "10000", 1), "the price and the lot")
    t.eq(LV._b64(bytes.fromhex("11" * 32)), o["maker_prog"], "the treasury's program")
    t.eq(o["merkle_path"], [LV._b64(K.tapleaf_hash(s.cov.reclaim_leaf))], "the FILL leaf's path is the reclaim leaf's hash")
    v0 = C.SaleTerms(GOLD, USDX, 3, 2, "11" * 20, 10_000, 2_000_000_000, "22" * 32, TOTAL, treasury_ver=0)
    s0 = S.Sale("v0", v0, "issuer")
    s0.confirm_lock(FUND, 1, s0.script_pubkey, TOTAL, GOLD)
    t.ok("version-0" in (LV.fillable(s0) or ""), "a version-0 treasury cannot be filled by a round", LV.fillable(s0))
    s1 = S.Sale("draft", s.terms, "issuer")
    t.ok("not resting" in (LV.fillable(s1) or ""), "a sale not on the chain cannot be")


def test_a_take_is_read_against_the_sale(t):
    s = _sale()
    got = LV.read_take(_status(s, "aa" * 32, 100_001, rnd=ROUND, k=1, round_filled=1_450_010), s)
    t.eq((got["token_atoms"], got["payment_atoms"], got["round"], got["input"], got["round_filled"]),
         (100_001, s.terms.cost_for(100_001), ROUND, 1, 1_450_010), "share, price, round, input, the round's fill")
    other = _status(s, "aa" * 32, 100_001)
    other["fill"]["script"] = "5120" + "99" * 32
    try:
        LV.read_take(other, s)
        t.ok(False, "a take of another order is refused")
    except LV.LeafError as e:
        t.ok("not of this sale" in str(e), "a take of another order is refused", str(e))
    cheap = _status(s, "aa" * 32, 100_001)
    cheap["fill"]["price"] = str(s.terms.cost_for(100_001) - 1)
    try:
        LV.read_take(cheap, s)
        t.ok(False, "a take priced under the covenant is refused")
    except LV.LeafError as e:
        t.ok("prices its fill" in str(e), "a take priced under the covenant is refused", str(e))


def test_the_round_is_checked_on_the_chain(t):
    s = _sale()
    take = LV.read_take(_status(s, "aa" * 32, 100_001, rnd=ROUND, k=1, round_filled=300_002), s)
    raw = _round(s, 1, 300_002)
    t.eq(LV.check_round(raw, s, take, 300_002), 300_002, "the sale at input 1, credited at 2, resting at 3")
    for what, bad, needle in (
            ("another outpoint at input 1", _round(s, 1, 300_002, order=("ff" * 32, 0)), "is not the sale"),
            ("a credit under the price of the round's fill", _round(s, 1, 300_002, credit=s.terms.cost_for(300_002) - 1), "less than the price"),
            ("a fill other than the operator says", _round(s, 1, 200_002), "the operator says")):
        try:
            LV.check_round(bad, s, take, 100_001)
            t.ok(False, what)
        except LV.LeafError as e:
            t.ok(needle in str(e), what, str(e))
    try:
        LV.check_round(raw, s, take, 300_003)
        t.ok(False, "shares that add up to more than the round filled")
    except LV.LeafError as e:
        t.ok("less than the" in str(e), "shares that add up to more than the round filled", str(e))


def test_a_take_counts_once_its_round_is_in_a_block(t):
    s = _sale()

    class Tier:
        cap_atoms = 10_000_000

    e = s.note_take("aa" * 32, "buyer", 100_000, 150_000, {"txid": FUND, "vout": 0}, TOTAL)
    t.eq(s.allowance_for("buyer", Tier), 10_000_000 - 150_000, "a take on its way holds its buyer's headroom")
    t.eq(s.allocations.get("buyer", 0), 0, "and is no purchase yet")
    s.take_confirmed(e, ROUND, 1, "block-120", 120)
    t.eq((s.allocations["buyer"], s.purchases["buyer"][0]["take"], s.purchases["buyer"][0]["input"]), (150_000, "aa" * 32, 1),
         "confirmed: a purchase made with the round, at its input")
    t.eq(s.allowance_for("buyer", Tier), 10_000_000 - 150_000, "counted once")
    t.eq(s.to_json()["buyers"], 1, "a buyer")
    s.take_undone(e)
    t.eq((s.allocations["buyer"], s.purchases["buyer"][0].get("undone")), (0, True), "its block left the chain: undone")
    t.eq(s.to_json()["buyers"], 0, "no buyer while undone")
    t.eq(s.allowance_for("buyer", Tier), 10_000_000 - 150_000, "still holding the headroom while it may return")
    s.take_confirmed(e, ROUND, 1, "block-120b", 120)
    t.eq((s.allocations["buyer"], s.purchases["buyer"][0].get("undone"), len(s.purchases["buyer"])), (150_000, None, 1),
         "mined again: it counts again, once")
    s.take_void(e, "gone")
    t.eq((s.allocations["buyer"], s.allowance_for("buyer", Tier)), (0, 10_000_000), "void: nothing counts or is held")


def _ten(s, op, rnd=None, k=1, state="issued"):
    fills = [100_001 + 10_000 * i for i in range(10)]
    for i, f in enumerate(fills):
        take = ("%02x" % i) * 32
        if s.take(take) is None:
            s.note_take(take, "buyer %d" % i, f, s.terms.cost_for(f), {"txid": FUND, "vout": 0}, TOTAL)
        op.statuses[take] = _status(s, take, f, state=state, rnd=rnd, k=k, round_filled=sum(fills) if rnd else None)
    return fills


def test_ten_buyers_one_round_and_an_anchor_rollback(t):
    s = _sale()
    node, op = Node(), Operator()
    node.txouts[(FUND, 0)] = {"value": TOTAL / 1e8, "confirmations": 6, "scriptPubKey": {"hex": s.script_pubkey}}
    node.blocks[100] = "block-100"
    w = W.Watcher(FakeMarket({"t": P(s)}), node, interval=1)
    w.market.projects["t"].slug = "t"
    w.market.leaf_operator = op
    fills = _ten(s, op)
    w.poll()
    t.eq({x["state"] for x in s.takes}, {S.TAKE_PENDING}, "taken, no round yet")

    # The operator builds the round and holds it for the takes' forfeits:
    # the node knows nothing of it.
    _ten(s, op, rnd=ROUND)
    w.poll()
    t.eq({x["state"] for x in s.takes}, {S.TAKE_PENDING}, "a round the node does not hold is not in the chain")

    # Broadcast: in the mempool.
    raw = _round(s, 1, sum(fills))
    node.txs[ROUND] = dict(raw)
    node.mempool = [ROUND]
    del node.txouts[(FUND, 0)]
    node.txouts[(ROUND, 3)] = {"value": (TOTAL - sum(fills)) / 1e8, "confirmations": 0, "asset": GOLD,
                               "scriptPubKey": {"hex": s.script_pubkey}}
    w.poll()
    t.eq({x["state"] for x in s.takes}, {S.TAKE_IN_ROUND}, "in the mempool")
    t.eq(s.allocations, {}, "nothing counts yet")
    t.eq((s.funding["txid"], s.funding["vout"], s.locked_atoms), (ROUND, 3, TOTAL - sum(fills)),
         "the sale rests again at the round's 2k+1")

    # Mined at 101.
    node.height = 101
    node.blocks[101] = "block-101"
    node.headers["block-101"] = 101
    node.txs[ROUND] = dict(raw, blockhash="block-101", confirmations=1)
    node.mempool = []
    node.txouts[(ROUND, 3)]["confirmations"] = 1
    w.poll()
    t.eq({x["state"] for x in s.takes}, {S.TAKE_CONFIRMED}, "in a block: all ten confirmed")
    t.eq(sorted(s.allocations.values()), sorted(s.terms.cost_for(f) for f in fills), "each buyer its own share's price")
    t.eq(s.to_json()["buyers"], 10, "ten buyers")
    t.eq(s.sold_atoms, sum(fills), "the sale's progress")

    # An anchor rollback takes block 101 away and the chain is one block
    # shorter: the node no longer has a block at the round's height, and the
    # round is in the mempool again.
    node.height = 100
    del node.blocks[101]
    node.txs[ROUND] = dict(raw)
    node.mempool = [ROUND]
    node.txouts[(ROUND, 3)]["confirmations"] = 0
    w.poll()
    t.eq({x["state"] for x in s.takes}, {S.TAKE_IN_ROUND}, "the block left the chain: back in the mempool, counting nothing")
    t.eq(sum(s.allocations.values()), 0, "every share undone")
    t.eq(s.to_json()["buyers"], 0, "no buyer")

    # Mined again, in another block 101: every attribution returns.
    node.height = 101
    node.blocks[101] = "block-101b"
    node.headers["block-101b"] = 101
    node.txs[ROUND] = dict(raw, blockhash="block-101b", confirmations=1)
    node.mempool = []
    node.txouts[(ROUND, 3)]["confirmations"] = 1
    w.poll()
    t.eq({(x["state"], x["block"]) for x in s.takes}, {(S.TAKE_CONFIRMED, "block-101b")}, "mined again: confirmed again")
    # A rollback that replaces the block at the same height is seen too.
    node.blocks[101] = "block-101c"
    node.headers["block-101c"] = 101
    node.txs[ROUND] = dict(raw)
    node.mempool = [ROUND]
    w.poll()
    t.eq({x["state"] for x in s.takes}, {S.TAKE_IN_ROUND}, "another block at its height: undone again")
    node.height = 102
    node.blocks[102] = "block-102"
    node.headers["block-102"] = 102
    node.txs[ROUND] = dict(raw, blockhash="block-102", confirmations=1)
    node.mempool = []
    w.poll()
    t.eq({(x["state"], x["block"]) for x in s.takes}, {(S.TAKE_CONFIRMED, "block-102")}, "and back at 102")
    t.eq(sorted(s.allocations.values()), sorted(s.terms.cost_for(f) for f in fills), "each share counts again, once")
    t.eq(sum(len(v) for v in s.purchases.values()), 10, "ten purchases, not twenty")


def test_a_silent_take_is_dropped_and_the_round_that_lies_is_not_believed(t):
    s = _sale()
    node, op = Node(), Operator()
    node.txouts[(FUND, 0)] = {"value": TOTAL / 1e8, "confirmations": 6, "scriptPubKey": {"hex": s.script_pubkey}}
    node.blocks[100] = "block-100"
    w = W.Watcher(FakeMarket({"t": P(s)}), node, interval=1)
    w.market.projects["t"].slug = "t"
    w.market.leaf_operator = op
    fills = _ten(s, op)
    silent = "09" * 32
    op.statuses[silent]["state"] = "void"
    op.statuses[silent]["void_reason"] = "its forfeits did not come within 600 s of its round being built"
    w.poll()
    t.eq(s.take(silent)["state"], S.TAKE_VOID, "the operator voided it")
    t.ok("600 s" in s.take(silent)["reason"], "and says why")
    # The nine in a round that claims to fill more than it does.
    nine = sum(fills[:9])
    for i in range(9):
        take = ("%02x" % i) * 32
        op.statuses[take] = _status(s, take, fills[i], rnd=ROUND, k=0, round_filled=nine)
    node.txs[ROUND] = dict(_round(s, 0, nine - 10_000), blockhash="block-101", confirmations=1)
    node.blocks[101] = "block-101"
    node.headers["block-101"] = 101
    node.height = 101
    w.poll()
    t.eq({s.take(("%02x" % i) * 32)["state"] for i in range(9)}, {S.TAKE_PENDING}, "a round filling less than the operator says counts nothing")
    node.txs[ROUND] = dict(_round(s, 0, nine), blockhash="block-101", confirmations=1)
    w.poll()
    t.eq({s.take(("%02x" % i) * 32)["state"] for i in range(9)}, {S.TAKE_CONFIRMED}, "the nine, served by one spend, confirmed")
    t.eq(s.allocations.get("buyer 9", 0), 0, "the silent buyer bought nothing")
