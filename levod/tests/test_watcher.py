"""Watcher checks: the chain is the source of truth about a sale.

The case that matters most is the last one. A sale that sold out and a sale
whose funding was reorged both leave nothing at the sale address, and they mean
opposite things: one is finished, the other was never funded. Getting that wrong
either hides a live sale or presents a phantom one as complete.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import covenant as C  # noqa: E402
import sale as S  # noqa: E402
import watcher as W  # noqa: E402

USDX = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"
GOLD = "3a0f9192219db59f8d7f87d93ac6311095dfe1255d149727b87baaa7d2cc71a1"
TOTAL = 1000 * 10**8


class FakeRPC:
    def __init__(self):
        self.unspents = []
        self.txouts = {}
        self.blocks = {}          # height -> hash currently at that height
        self.height = 100

    def call(self, method, *params):
        if method == "scantxoutset":
            return {"success": True, "unspents": list(self.unspents)}
        if method == "getblockhash":
            return self.blocks.get(int(params[0]))
        if method == "getrawtransaction":
            return None
        raise RuntimeError("unexpected call %s" % method)

    def txout(self, txid, vout, include_mempool=True):
        return self.txouts.get((txid, int(vout)))

    def chain_height(self):
        return self.height


class FakeMarket:
    def __init__(self, projects):
        self.projects = projects
        self.saved = 0

    def save(self):
        self.saved += 1


class P:
    def __init__(self, sale):
        self.sale = sale


def _sale():
    terms = C.SaleTerms(GOLD, USDX, 1, 4, "11" * 32, 10 * 10**8, 2_000_000_000,
                        "22" * 32, TOTAL)
    s = S.Sale("t", terms, "issuer")
    s.confirm_lock("ab" * 32, 0, s.script_pubkey, TOTAL, GOLD)
    return s


def _watch(sale, rpc):
    w = W.Watcher(FakeMarket({"t": P(sale)}), rpc, interval=1)
    w.market.projects["t"].slug = "t"
    return w


def _settle(sale, rpc):
    """Poll until the watcher is willing to call a sale finished.

    It deliberately will not do so on a single blind reading, nor on two
    readings of the same block: the confirmed-set scan cannot see a mempool
    remainder, so silence means "look again" until a new block has had the
    chance to carry one.
    """
    w = _watch(sale, rpc)
    _twice(w, rpc)
    return w


def _twice(w, rpc):
    """Two polls with a block between them: the least that can end a sale."""
    w.poll()
    rpc.height += 1
    w.poll()


def test_unchanged_sale_stays_live(t):
    s = _sale()
    rpc = FakeRPC()
    rpc.unspents = [{"txid": "ab" * 32, "vout": 0, "scriptPubKey": s.script_pubkey,
                     "amount": TOTAL / 1e8, "asset": GOLD}]
    _watch(s, rpc).poll()
    t.eq(s.status, S.LIVE, "a full covenant is a live sale")
    t.eq(s.locked_atoms, TOTAL, "holding everything")
    t.eq(s.sold_atoms, 0, "nothing sold")


def test_a_partial_buy_is_picked_up_without_being_told(t):
    """The sell leaf is permissionless, so purchases happen that Levo never
    planned. The remainder re-rests at the same address, and the watcher simply
    finds a smaller output at a new outpoint."""
    s = _sale()
    rpc = FakeRPC()
    rpc.unspents = [{"txid": "cd" * 32, "vout": 1, "scriptPubKey": s.script_pubkey,
                     "amount": 600.0, "asset": GOLD}]
    _watch(s, rpc).poll()
    t.eq(s.status, S.PARTIAL, "a smaller resting output is a partial sale")
    t.eq(s.locked_atoms, 600 * 10**8, "locked follows the chain")
    t.eq(s.sold_atoms, 400 * 10**8, "and sold is the difference")
    t.eq(s.funding["txid"], "cd" * 32, "the sale moves to the new outpoint")
    t.eq(s.funding["vout"], 1, "including its index")


def test_selling_out_is_recognised(t):
    """The sale was mined in a block that is still in the chain, and now holds
    nothing: it sold out."""
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()                     # observe it confirmed
    t.eq(s.funding["block"], "block-95", "the watcher notes where it was mined")
    del rpc.txouts[("ab" * 32, 0)]            # spent
    _settle(s, rpc)
    t.eq(s.status, S.SOLD_OUT, "nothing left resting, block intact: sold out")
    t.eq(s.locked_atoms, 0, "and it holds nothing")


def test_a_sold_out_sale_is_never_called_a_reorg(t):
    """A node without -txindex cannot find a fully spent transaction, so
    treating 'not found' as proof of a reorg would tell buyers a sold-out sale
    was never funded. Only the block going missing is evidence."""
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    del rpc.txouts[("ab" * 32, 0)]
    _settle(s, rpc)
    t.eq(s.status, S.SOLD_OUT, "still sold out, however unfindable the tx is")


def test_a_reorg_is_detected_by_the_block_going_missing(t):
    """Sequentia follows its Bitcoin anchor, so the block a funding was mined
    into can stop being the block at that height. That is the evidence."""
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    del rpc.txouts[("ab" * 32, 0)]
    rpc.blocks[95] = "a-different-block"      # reorged
    _settle(s, rpc)
    t.eq(s.status, S.GHOST, "a replaced block means the funding went with it")


def test_a_reorged_lock_becomes_a_ghost_not_a_sellout(t):
    """Sequentia follows its Bitcoin anchor, so funding can be un-made after a
    sale was already open. Nothing rests at the address in that case either --
    but the sale was never funded, and must stop being investable rather than
    showing as complete."""
    s = _sale()
    rpc = FakeRPC()
    rpc.unspents = []                 # nothing resting, never seen confirmed
    _settle(s, rpc)
    t.eq(s.status, S.GHOST, "funding never seen confirmed and now absent")
    t.eq(s.funding, None, "a ghost has no outpoint")
    t.eq(s.locked_atoms, 0, "and holds nothing")


def test_a_ghost_recovers_when_it_is_funded_again(t):
    s = _sale()
    rpc = FakeRPC()
    _settle(s, rpc)
    t.eq(s.status, S.GHOST, "ghosted first")
    rpc.unspents = [{"txid": "ef" * 32, "vout": 0, "scriptPubKey": s.script_pubkey,
                     "amount": TOTAL / 1e8, "asset": GOLD}]
    _watch(s, rpc).poll()
    t.eq(s.status, S.LIVE, "a re-funded sale comes back")
    t.eq(s.funding["txid"], "ef" * 32, "at its new outpoint")


def test_a_stray_payment_cannot_shrink_a_sale(t):
    """Anyone can pay the sale address. The covenant only ever rests one output
    there, so the watcher takes the largest rather than whichever came last."""
    s = _sale()
    rpc = FakeRPC()
    rpc.unspents = [
        {"txid": "11" * 32, "vout": 0, "scriptPubKey": s.script_pubkey,
         "amount": 0.001, "asset": GOLD},
        {"txid": "cd" * 32, "vout": 1, "scriptPubKey": s.script_pubkey,
         "amount": 600.0, "asset": GOLD},
    ]
    _watch(s, rpc).poll()
    t.eq(s.locked_atoms, 600 * 10**8, "the real resting output wins")


def test_a_different_asset_at_the_address_is_ignored(t):
    s = _sale()
    rpc = FakeRPC()
    rpc.unspents = [{"txid": "22" * 32, "vout": 0, "scriptPubKey": s.script_pubkey,
                     "amount": 5.0, "asset": USDX}]
    _settle(s, rpc)
    t.eq(s.status, S.GHOST, "an unrelated asset is not the sale token")


def test_drafts_are_left_alone(t):
    terms = C.SaleTerms(GOLD, USDX, 1, 4, "11" * 32, 10 * 10**8, 2_000_000_000,
                        "22" * 32, TOTAL)
    s = S.Sale("t", terms, "issuer")
    rpc = FakeRPC()
    _settle(s, rpc)
    t.eq(s.status, S.DRAFT, "an unfunded draft is not a ghost")


def test_a_freshly_locked_sale_is_not_read_as_sold_out(t):
    """`scantxoutset` walks the CONFIRMED set only, so a sale funded a moment
    ago is invisible to it. Concluding from that silence that the sale is over
    would take a live sale off the board within a minute of it opening -- which
    is exactly what happened the first time a sale was listed on the deployed
    platform."""
    s = _sale()
    rpc = FakeRPC()
    rpc.unspents = []                                  # not yet confirmed
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8}   # but live in the mempool
    _settle(s, rpc)
    t.eq(s.status, S.LIVE, "an unconfirmed lock still reads as a live sale")
    t.eq(s.locked_atoms, TOTAL, "holding everything it was funded with")


def test_one_blind_reading_is_not_enough_to_end_a_sale(t):
    """A buy's remainder sits in the mempool for a block or two, where the scan
    cannot see it either. One silent poll means look again."""
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    w = _watch(s, rpc)
    w.poll()                                   # seen, confirmed, block noted
    del rpc.txouts[("ab" * 32, 0)]             # now spent
    w.poll()
    t.eq(s.status, S.LIVE, "still live after a single silent poll")
    w.poll()
    t.eq(s.status, S.LIVE, "and still live after two silent polls on one block")
    rpc.height += 1
    w.poll()
    t.eq(s.status, S.SOLD_OUT, "and finished once a new block has said the same")


def test_the_recorded_outpoint_is_authoritative_over_the_scan(t):
    """Asking about the outpoint we already know is both cheaper and sees the
    mempool, so it decides; the scan is what finds a sale that MOVED."""
    s = _sale()
    rpc = FakeRPC()
    rpc.txouts[("ab" * 32, 0)] = {"value": 700.0}
    rpc.unspents = [{"txid": "zz", "vout": 9, "scriptPubKey": s.script_pubkey,
                     "amount": 1.0, "asset": GOLD}]
    _watch(s, rpc).poll()
    t.eq(s.locked_atoms, 700 * 10**8, "the known outpoint decides")
    t.eq(s.funding["txid"], "ab" * 32, "and the sale stays where it is")


def test_selling_out_after_a_scanned_partial_is_not_a_reorg(t):
    """A remainder found by the confirmed-set scan WAS observed confirmed: the
    scan cannot see anything else. Forgetting that turned every sale that sold
    out shortly after a partial buy into a 'reorged lock' -- the scan path
    moved the sale to its new outpoint without noting the block, and the next
    silence read as 'never seen confirmed'."""
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[97] = "block-97"
    rpc.unspents = [{"txid": "cd" * 32, "vout": 1, "scriptPubKey": s.script_pubkey,
                     "amount": 600.0, "asset": GOLD, "height": 97}]
    _watch(s, rpc).poll()
    t.eq(s.status, S.PARTIAL, "the partial buy is picked up")
    t.eq(s.funding.get("block"), "block-97", "and the block it confirmed in is noted")
    rpc.unspents = []                          # the rest sold, no remainder
    _settle(s, rpc)
    t.eq(s.status, S.SOLD_OUT, "selling out afterwards is a sell-out, not a reorg")
    t.eq(s.sold_atoms, TOTAL, "with everything sold")


def test_emptied_after_the_close_is_closed_not_sold_out(t):
    """After the close both leaves can spend the covenant, so an empty covenant
    is not evidence of a sale. The sale is closed with nothing left, and the
    sold figure stays at what was last observed."""
    s = _sale()
    s.terms.close_locktime = 90                # a height; the chain is at 100
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    del rpc.txouts[("ab" * 32, 0)]
    _settle(s, rpc)
    t.eq(s.status, S.CLOSED, "emptied after the close: closed, not sold out")
    t.eq(s.locked_atoms, 0, "holding nothing")
    t.eq(s.sold_atoms, 0, "and nothing is claimed to have sold")


def test_a_reclaim_that_landed_is_reported_as_reclaimed(t):
    """A reclaim Levo built pays the swept tokens to output 0 of a known
    transaction. Seeing that output is proof the project took them back."""
    s = _sale()
    s.terms.close_locktime = 90
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    s.note_reclaim("77" * 32)
    del rpc.txouts[("ab" * 32, 0)]
    rpc.txouts[("77" * 32, 0)] = {"value": TOTAL / 1e8, "asset": GOLD}
    _settle(s, rpc)
    t.eq(s.status, S.RECLAIMED, "the reclaim's output is on chain: reclaimed")
    t.eq(s.locked_atoms, 0, "and the covenant is empty")


def test_a_recorded_purchase_moves_the_sale_before_it_confirms(t):
    """The scan sees confirmed outputs only, but `gettxout` on a named outpoint
    sees the mempool. A purchase recorded through Levo names its transaction,
    so the remainder at its output 1 is found the moment it is broadcast."""
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    del rpc.txouts[("ab" * 32, 0)]                        # spent by the buy
    s.expect_remainder_at("ee" * 32)
    rpc.txouts[("ee" * 32, 1)] = {"value": 250.0, "asset": GOLD,
                                  "scriptPubKey": {"hex": s.script_pubkey}}
    _watch(s, rpc).poll()
    t.eq(s.status, S.PARTIAL, "the sale moved on one poll, unconfirmed")
    t.eq(s.funding["txid"], "ee" * 32, "to the purchase's remainder output")
    t.eq(s.locked_atoms, 250 * 10**8, "with what it now holds")
    t.eq(s.candidates, [], "and the hint is spent")


def test_a_candidate_holding_something_else_is_ignored(t):
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    del rpc.txouts[("ab" * 32, 0)]
    s.expect_remainder_at("ee" * 32)
    rpc.txouts[("ee" * 32, 1)] = {"value": 250.0, "asset": USDX,
                                  "scriptPubKey": {"hex": s.script_pubkey}}
    _settle(s, rpc)
    t.eq(s.status, S.SOLD_OUT, "a different asset at output 1 is not a remainder")


def test_the_mempool_is_searched_for_a_spend_of_the_known_outpoint(t):
    """A buy made without Levo leaves no hint. While its remainder is
    unconfirmed the only place it exists is the mempool, so that is read."""
    s = _sale()

    class MempoolRPC(FakeRPC):
        def call(self, method, *params):
            if method == "getrawmempool":
                return ["dd" * 32, "ee" * 32]
            if method == "getrawtransaction":
                if params[0] == "ee" * 32:
                    return {"txid": "ee" * 32,
                            "vin": [{"txid": "ab" * 32, "vout": 0}],
                            "vout": [
                                {"n": 0, "value": 100.0, "asset": USDX,
                                 "scriptPubKey": {"hex": "5120" + "11" * 32}},
                                {"n": 1, "value": 600.0, "asset": GOLD,
                                 "scriptPubKey": {"hex": s.script_pubkey}},
                            ]}
                return {"txid": "dd" * 32, "vin": [{"txid": "00" * 32, "vout": 0}],
                        "vout": []}
            return super().call(method, *params)

    rpc = MempoolRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    del rpc.txouts[("ab" * 32, 0)]
    _watch(s, rpc).poll()
    t.eq(s.status, S.PARTIAL, "the mempool spend is found on one poll")
    t.eq(s.funding["txid"], "ee" * 32, "and the sale follows it")
    t.eq(s.locked_atoms, 600 * 10**8, "holding the re-rested remainder")


def test_a_reclaimed_sale_is_left_alone(t):
    s = _sale()
    s.terms.close_locktime = 90
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    s.note_reclaim("77" * 32)
    del rpc.txouts[("ab" * 32, 0)]
    rpc.txouts[("77" * 32, 0)] = {"value": TOTAL / 1e8, "asset": GOLD}
    w = _settle(s, rpc)
    t.eq(s.status, S.RECLAIMED, "reclaimed")
    t.eq(w.poll()["checked"], 0, "a reclaimed sale is finished; nothing to watch")


def test_a_ghost_stays_a_ghost_until_it_is_funded_again(t):
    """A ghost has no outpoint. Reading its continued absence as a sell-out
    would announce that a sale nobody could buy from sold everything."""
    s = _sale()
    rpc = FakeRPC()
    w = _settle(s, rpc)
    t.eq(s.status, S.GHOST, "ghosted")
    for _ in range(4):
        _twice(w, rpc)
    t.eq(s.status, S.GHOST, "and still a ghost however often the watcher looks")


def test_a_sold_out_sale_stays_sold_out_after_the_close_passes(t):
    s = _sale()
    s.terms.close_locktime = 200                # a height; the chain is at 100
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    del rpc.txouts[("ab" * 32, 0)]
    w = _settle(s, rpc)
    t.eq(s.status, S.SOLD_OUT, "sold out before the close")
    rpc.height = 300                           # the close date comes and goes
    _twice(w, rpc)
    t.eq(s.status, S.SOLD_OUT, "sold out is final; the close passing changes nothing")


def test_a_closed_empty_sale_becomes_reclaimed_when_the_reclaim_lands(t):
    s = _sale()
    s.terms.close_locktime = 90
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    _watch(s, rpc).poll()
    del rpc.txouts[("ab" * 32, 0)]
    w = _settle(s, rpc)
    t.eq(s.status, S.CLOSED, "closed and empty, with no reclaim known")
    s.note_reclaim("77" * 32)
    rpc.txouts[("77" * 32, 0)] = {"value": TOTAL / 1e8, "asset": GOLD}
    _twice(w, rpc)
    t.eq(s.status, S.RECLAIMED, "the reclaim shows up later and the sale says so")


def test_two_silent_polls_on_one_block_do_not_end_a_sale(t):
    """A buy's remainder sits in the mempool until a block carries it, and no
    number of polls on the same block can see it. Only a new block that still
    shows nothing is evidence."""
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    w = _watch(s, rpc)
    w.poll()
    del rpc.txouts[("ab" * 32, 0)]
    for _ in range(5):
        w.poll()
    t.eq(s.status, S.LIVE, "five silent polls on one block change nothing")
    rpc.height += 1
    w.poll()
    t.eq(s.status, S.SOLD_OUT, "one silent poll after a new block ends it")


def test_a_failed_scan_changes_nothing(t):
    """`scantxoutset` answers success:false when another scan is running. That
    is 'unknown', not 'nothing there'."""
    s = _sale()

    class FlakyRPC(FakeRPC):
        def call(self, method, *params):
            if method == "scantxoutset":
                return {"success": False}
            return super().call(method, *params)

    rpc = FlakyRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    w = _watch(s, rpc)
    w.poll()
    del rpc.txouts[("ab" * 32, 0)]
    for _ in range(3):
        _twice(w, rpc)
    t.eq(s.status, S.LIVE, "a sale is never ended on a scan that did not complete")
    t.ok(w.last_error is not None, "and the watcher says the scan failed")


def test_a_same_height_block_replacement_is_not_a_reorg_of_the_funding(t):
    """A one-block reorg replaces the block at a height and usually carries
    the funding again. The note must follow the chain, or a later sell-out
    reads as a ghost."""
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[95] = "block-A"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    w = _watch(s, rpc)
    w.poll()
    t.eq(s.funding["block"], "block-A", "noted in block A")
    rpc.blocks[95] = "block-B"                 # replaced; funding still there
    w.poll()
    t.eq(s.funding["block"], "block-B", "the note follows the chain")
    del rpc.txouts[("ab" * 32, 0)]
    _twice(w, rpc)
    t.eq(s.status, S.SOLD_OUT, "selling out afterwards is a sell-out")


def test_a_chain_shorter_than_the_funding_height_is_a_reorg(t):
    s = _sale()
    rpc = FakeRPC()
    rpc.blocks[95] = "block-95"
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    w = _watch(s, rpc)
    w.poll()
    del rpc.txouts[("ab" * 32, 0)]
    del rpc.blocks[95]
    rpc.height = 90                            # the chain no longer reaches 95
    w.poll()
    rpc.height = 91
    w.poll()
    t.eq(s.status, S.GHOST, "a chain that no longer reaches the funding height lost it")


def test_a_failing_sale_is_reported(t):
    s = _sale()

    class BrokenRPC(FakeRPC):
        def txout(self, txid, vout, include_mempool=True):
            raise RuntimeError("node away")

    rpc = BrokenRPC()
    w = _watch(s, rpc)
    w.poll()
    t.ok(w.last_error and "node away" in w.last_error, "the error reaches last_error")
    t.eq(s.status, S.LIVE, "and the sale is left as it was")


def test_the_scan_is_skipped_when_every_sale_answers(t):
    s = _sale()

    class CountingRPC(FakeRPC):
        scans = 0

        def call(self, method, *params):
            if method == "scantxoutset":
                CountingRPC.scans += 1
            return super().call(method, *params)

    rpc = CountingRPC()
    rpc.txouts[("ab" * 32, 0)] = {"value": TOTAL / 1e8, "confirmations": 6}
    w = _watch(s, rpc)
    r = w.poll()
    t.eq(CountingRPC.scans, 0, "no UTXO-set scan when the known outpoint answers")
    t.eq(r["scanned"], False, "and the poll says so")


def test_other_assets_at_the_sale_address_are_reported_as_strays(t):
    """The sell leaf does not check what asset it spends, so anything else at
    the address can be taken at the sale's price. The project is told."""
    s = _sale()
    rpc = FakeRPC()
    rpc.unspents = [
        {"txid": "cd" * 32, "vout": 1, "scriptPubKey": s.script_pubkey, "amount": 600.0, "asset": GOLD},
        {"txid": "99" * 32, "vout": 0, "scriptPubKey": s.script_pubkey, "amount": 100.0, "asset": USDX},
    ]
    _watch(s, rpc).poll()
    t.eq(s.strays, [{"txid": "99" * 32, "vout": 0, "asset": USDX, "atoms": 100 * 10**8}],
         "the stray USDX is recorded")
    t.eq(s.locked_atoms, 600 * 10**8, "and does not count as the sale")
