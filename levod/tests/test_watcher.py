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

    def call(self, method, *params):
        if method == "scantxoutset":
            return {"success": True, "unspents": list(self.unspents)}
        if method == "getrawtransaction":
            return None
        raise RuntimeError("unexpected call %s" % method)

    def txout(self, txid, vout, include_mempool=True):
        return self.txouts.get((txid, int(vout)))

    def chain_height(self):
        return 100


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
    return W.Watcher(FakeMarket({"t": P(sale)}), rpc, interval=1)


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
    s = _sale()
    rpc = FakeRPC()
    rpc.unspents = []
    rpc.txouts[("ab" * 32, 1)] = {"value": 1.0}      # funding tx still on chain
    _watch(s, rpc).poll()
    t.eq(s.status, S.SOLD_OUT, "nothing left resting, funding intact: sold out")
    t.eq(s.locked_atoms, 0, "and it holds nothing")


def test_a_reorged_lock_becomes_a_ghost_not_a_sellout(t):
    """Sequentia follows its Bitcoin anchor, so funding can be un-made after a
    sale was already open. Nothing rests at the address in that case either --
    but the sale was never funded, and must stop being investable rather than
    showing as complete."""
    s = _sale()
    rpc = FakeRPC()
    rpc.unspents = []                 # nothing resting
    # and nothing of the funding transaction survives anywhere
    _watch(s, rpc).poll()
    t.eq(s.status, S.GHOST, "a vanished funding transaction is a ghost")
    t.eq(s.funding, None, "a ghost has no outpoint")
    t.eq(s.locked_atoms, 0, "and holds nothing")


def test_a_ghost_recovers_when_it_is_funded_again(t):
    s = _sale()
    rpc = FakeRPC()
    _watch(s, rpc).poll()
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
    _watch(s, rpc).poll()
    t.eq(s.status, S.GHOST, "an unrelated asset is not the sale token")


def test_drafts_are_left_alone(t):
    terms = C.SaleTerms(GOLD, USDX, 1, 4, "11" * 32, 10 * 10**8, 2_000_000_000,
                        "22" * 32, TOTAL)
    s = S.Sale("t", terms, "issuer")
    rpc = FakeRPC()
    _watch(s, rpc).poll()
    t.eq(s.status, S.DRAFT, "an unfunded draft is not a ghost")
