"""The receiver's vesting lots, against stand-ins for the node, the Lightning
node and the project's Arca wallet.

A vesting sale's lot is paid as an Arca vesting claim to the buyer's key, and
the payment is settled only once the operator has released that claim. These
checks follow the receiver's states (stocking, boarding, distributing,
delivered) and its refusals; test_lightning_e2e.py and the VL1 regtest run
drive the same code against real daemons.
"""

import hashlib
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import covenant as C  # noqa: E402
import receiver as RCV  # noqa: E402
import secp256k1 as EC  # noqa: E402
import tx as TX  # noqa: E402

SECRET = int("44" * 32, 16)
R = EC.xonly_pubkey(SECRET)
RECLAIM = EC.xonly_pubkey(int("22" * 32, 16))
TOKEN, BOND = "aa" * 32, "bb" * 32
BUYER = "02" + EC.xonly_pubkey(int("33" * 32, 16)).hex()
NOW = 1_800_000_000
VEST = NOW + 30 * 86400
ARCA_SPK = "0014" + "55" * 20


def terms(vest=VEST):
    ln = {"receiver_xonly": R.hex(), "receiver_url": "http://127.0.0.1:8098", "btc_num": 1,
          "btc_den": 1, "bond_asset": BOND, "bond_atoms": 20000, "fee_allowance": 10000,
          "vest_locktime": vest}
    return C.SaleTerms(TOKEN, BOND, 1, 4, "11" * 32, 1000, 1_000_000, RECLAIM.hex(), lightning=ln)


class Node:
    def __init__(self):
        self.sent = []
        self.conf = {}
        self.median = NOW

    def call(self, method, *a):
        if method == "getblockchaininfo":
            return {"mediantime": self.median}
        if method == "getblockhash":
            return "00" * 32
        if method == "sendrawtransaction":
            t = TX.Transaction.from_hex(a[0]) if hasattr(TX.Transaction, "from_hex") else None
            txid = hashlib.sha256(a[0].encode()).hexdigest()
            self.sent.append((txid, a[0], t))
            return txid
        if method == "scantxoutset":
            spk = a[1][0][4:-1]
            if spk == "5120" + R.hex():
                return {"unspents": [{"txid": "cc" * 32, "vout": 0, "asset": BOND, "amount": 1.0}]}
            return {"unspents": []}
        raise AssertionError("unexpected node call %s" % method)

    def txout(self, txid, vout):
        if txid in ("cc" * 32, "dd" * 32):
            return {"confirmations": 5}
        if txid in self.conf:
            return {"confirmations": self.conf[txid]}
        return None


class Ln:
    def __init__(self):
        self.state = "open"
        self.settled = []
        self.cancelled = []

    def hold(self, h, msat):
        pass

    def invoice(self, msat, label, desc, expiry, preimage):
        return {"payment_hash": hashlib.sha256(bytes.fromhex(preimage)).hexdigest(),
                "bolt11": "lnbcrt1", "expires_at": NOW + expiry}

    def held(self, h):
        return {"state": self.state, "received_msat": 10 ** 12}

    def settle(self, h, preimage):
        self.settled.append(h)

    def cancel(self, h):
        self.cancelled.append(h)


class Arca:
    def __init__(self):
        self.stock = 0
        self.calls = []
        self.claim_state = "pending"

    def address(self):
        return {"address": "tb1q", "script_pubkey": ARCA_SPK, "key": "55" * 32}

    def sync(self):
        self.calls.append("sync")
        return {}

    def board(self, asset, atoms, fee_asset):
        self.calls.append(("board", asset, atoms, fee_asset))
        self.stock += atoms
        return {}

    def quote(self, asset, key, atoms):
        self.calls.append(("quote", key, atoms))
        if self.stock < atoms + atoms // 100:
            raise RCV.ReceiverError("the wallet holds no live coin of asset %s" % asset)
        return {"fee": str(atoms // 100)}

    def distribute(self, asset, key, atoms, vest, days, fee):
        self.calls.append(("distribute", asset, key, atoms, vest, days, fee))
        return {"distribution": "d1"}

    def distributions(self):
        return [{"distribution_id": "d1", "claims": [{"state": self.claim_state}]}]


def make(t_=None, arca=None):
    node, ln, arca = Node(), Ln(), arca if arca is not None else Arca()
    sale = t_ or terms()
    funding = {"txid": "dd" * 32, "vout": 0, "atoms": 1_000_000}
    path = Path(tempfile.mkdtemp()) / "state.json"
    r = RCV.Receiver(SECRET, ln, node, lambda slug: (sale, funding), path, arca=arca,
                     now=lambda: NOW)
    return r, node, ln, arca


def order(r, buyer=BUYER):
    return r.invoice("s", 100_000, "0014" + "66" * 20, "0014" + "66" * 20, buyer)


def test_a_vesting_order_needs_the_buyers_key(t):
    r, *_ = make()
    for bad, what in ((None, "no key"), ("02" + "00" * 32, "no curve point"), ("04" + "11" * 32, "no compressed key")):
        try:
            order(r, bad)
            t.ok(False, "a vesting order with %s is refused" % what)
        except RCV.ReceiverError:
            t.ok(True, "a vesting order with %s is refused" % what)
    r2, *_ = make(arca=False)
    r2.arca = None
    try:
        order(r2)
        t.ok(False, "a receiver without an Arca wallet refuses a vesting order")
    except RCV.ReceiverError as e:
        t.ok("Arca wallet" in str(e), "a receiver without an Arca wallet refuses a vesting order")


def test_a_vesting_lot_is_moved_in_boarded_and_paid_then_settled(t):
    r, node, ln, arca = make()
    inv = order(r)
    h = inv["payment_hash"]
    t.eq(r.poll(), [], "an unpaid order does nothing")
    ln.state = "accepted"
    out = r.poll()
    t.eq(out[0][0], "stocking", "a paid lot the Arca wallet cannot pay is moved in from the sale")
    rec = r.lots[h]
    txid, raw, _ = node.sent[-1]
    t.eq(rec["stock_txid"], txid, "the move is broadcast")
    t.eq(rec["stock_atoms"], 102_000, "the lot and a fiftieth more for the operator's fee")
    t.ok(ARCA_SPK in raw and ARCA_SPK in raw[raw.index(ARCA_SPK) + 1:],
         "the tokens and the board's fee coin go to the Arca wallet's address")
    t.ok(r.sale_coins["s"]["atoms"] == 1_000_000 - 102_000, "the rest stays in the sale")
    t.eq(r.poll(), [], "nothing more until the move is in a block")
    node.conf[txid] = 1
    out = r.poll()
    t.eq(out[0][0], "boarding", "once in a block, it is boarded")
    t.ok(("board", TOKEN, 102_000, BOND) in arca.calls, "the board pays its fee in the bond asset")
    out = r.poll()
    t.eq(out[0][0], "distributing", "once boarded, the lot is paid as a vesting claim")
    d = [c for c in arca.calls if c[0] == "distribute"][0]
    t.eq(d, ("distribute", TOKEN, BUYER, 100_000, VEST, 30, "1000"),
         "to the buyer's key, at the sale's vesting date, on the quoted fee")
    t.eq(r.poll(), [], "not settled while the claim is pending")
    t.eq(ln.settled, [], "the payment is still held")
    arca.claim_state = "vesting"
    out = r.poll()
    t.eq(out[0][0], "delivered", "settled once the operator has released the claim")
    t.eq(ln.settled, [h], "the payment is taken only then")
    st = r.status(h)
    t.eq(st["distribution_id"], "d1", "the status names the distribution")
    t.eq(st["vest"], VEST, "and the vesting date")


def test_stock_already_in_the_wallet_pays_at_once(t):
    arca = Arca()
    arca.stock = 500_000
    r, node, ln, arca = make(arca=arca)
    order(r)
    ln.state = "accepted"
    t.eq(r.poll()[0][0], "distributing", "a wallet that holds enough pays the lot at once")
    t.eq(node.sent, [], "nothing moves out of the sale")


def test_a_payment_that_goes_away_before_the_lot_is_paid(t):
    r, node, ln, arca = make()
    h = order(r)["payment_hash"]
    ln.state = "accepted"
    r.poll()
    ln.state = "cancelled"
    t.eq(r.poll()[0][0], "cancelled", "a payment gone before the claim is made cancels the lot")
    t.eq([c for c in arca.calls if c[0] == "distribute"], [], "and pays no claim")
    t.eq(ln.settled, [], "and settles nothing")
    t.eq(r.lots[h]["state"], "cancelled", "recorded")


def test_a_lot_whose_tokens_do_not_come_in_goes_back(t):
    r, node, ln, arca = make()
    h = order(r)["payment_hash"]
    ln.state = "accepted"
    r.poll()
    r.now = lambda: NOW + RCV.VEST_PATIENCE_SECONDS + 1
    t.eq(r.poll()[0][0], "cancelled", "a move that never confirms fails the payment back in time")
    t.eq(ln.cancelled, [h], "the payment is failed back")


def test_past_the_vesting_margin_a_lot_is_delivered_as_any(t):
    r, node, ln, arca = make(terms(vest=NOW + RCV.VEST_MARGIN_SECONDS - 60))
    inv = order(r)
    t.ok("vest" not in r.lots[inv["payment_hash"]], "a sale whose date is near delivers as any sale")
