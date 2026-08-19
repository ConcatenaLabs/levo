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

    It deliberately will not do so on a single blind reading: the confirmed-set
    scan cannot see a mempool remainder, so one silent poll means "look again",
    not "the sale is over".
    """
    w = _watch(sale, rpc)
    w.poll()
    w.poll()
    return w


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
    t.eq(s.status, S.SOLD_OUT, "and finished once it reads the same way twice")


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
