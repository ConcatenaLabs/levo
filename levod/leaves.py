"""Buying from leaves: an operator's round fills a sale for many buyers at once.

A Levo sale's covenant is the fill covenant of the order book the leaf
operator's rounds read, opcode for opcode, when its treasury is a taproot
(version-1) output: the sell leaf credits the treasury at output 2k for the
covenant spent at input k and re-rests the remainder at 2k+1, and it reads
nothing else. So a holder of leaves can buy from a sale without an on-chain
coin: its wallet asks the operator to take the sale (a "take"), the
operator's next round spends the sale once for every take of it together,
credits the treasury the sale's price for their sum, rests the remainder at
the same address, and pays each buyer a leaf of its own share in the round's
batch.

The covenant judges the round; Levo judges nothing it cannot read on the
chain. What the chain cannot say is which buyer took which share: the shares
are leaves inside one batch output. That comes from the operator, who serves
each take's status by its id. Levo reads it, records the take against the
account that named it, and then checks everything the chain can check: that
the round spends this sale at the input the operator names, pays this sale's
treasury at 2k at least the price of the round's whole fill, rests what is
left at this sale's address at 2k+1, and that the shares Levo records for it
add up to no more than it filled. A take counts against its buyer's
allocation only once its round is in a block of the chain, and stops counting
when that block leaves the chain: Sequentia follows its Bitcoin anchor, and a
round undone by an anchor rollback is a purchase undone until it is mined
again.

Levo holds no keys here either. The take is made and signed by the buyer's
wallet with the operator; Levo only hands the wallet the sale in the form the
operator reads (`order_form`), and only reads the operator's answers.
"""

import base64
import json
import re
import urllib.error
import urllib.request

import covenant as C
import script as K

TIMEOUT = 6
# The most of an operator's answer levod will read. A take's status is a few
# kilobytes.
MAX_ANSWER = 200_000
TAKE_ID = re.compile(r"^[0-9a-f]{64}$")


class LeafError(Exception):
    """The operator could not be asked, or answered something levod cannot use."""


class LeafOperator:
    """The operator whose rounds fill sales from leaves, read over HTTP."""

    def __init__(self, url, timeout=TIMEOUT):
        self.url = str(url).rstrip("/")
        self.timeout = timeout

    def _call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read(MAX_ANSWER + 1)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read(MAX_ANSWER + 1)
                err = json.loads(raw.decode() or "{}").get("error") or {}
            except Exception:
                err = {}
            if isinstance(err, dict) and err.get("code") == "unknown":
                raise LeafError("the operator knows no take %s" % (body or {}).get("participation_id", ""))
            raise LeafError("the operator answered %d: %s" % (e.code, (err.get("message") if isinstance(err, dict) else err) or e.reason))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LeafError("the operator at %s could not be reached: %s" % (self.url, e))
        if len(raw) > MAX_ANSWER:
            raise LeafError("the operator's answer is larger than levod reads")
        try:
            return json.loads(raw.decode())
        except ValueError as e:
            raise LeafError("the operator's answer is not JSON: %s" % e)

    def take(self, take_id):
        """A take's status, as the operator serves it to anyone holding its id."""
        take_id = str(take_id or "").lower()
        if not TAKE_ID.match(take_id):
            raise LeafError("a take is named by its 64-hex id")
        return self._call("/v1/participation_status", {"participation_id": take_id})

    def info(self):
        return self._call("/v1/info")


def fillable(sale):
    """Why this sale cannot be bought from leaves, or None when it can."""
    if sale is None:
        return "there is no sale"
    if getattr(sale, "capped", False):
        return ("a capped sale's covenant checks each buyer's key and cap, and an operator's round "
                "fills only the plain sale covenant")
    if sale.terms.treasury_ver != 1:
        return ("the treasury is a version-0 address, and the covenant an operator's round fills "
                "credits a taproot (version-1) treasury only")
    if not sale.funding:
        return "the sale is not resting on the chain"
    return None


def _b64(b):
    return base64.b64encode(bytes(b)).decode()


def order_form(sale):
    """The sale as an order in the form the operator and the buyer's wallet read
    it: the order book's `CovenantTerms` in its JSON form, at the outpoint the
    sale rests at. Asset ids go in the order the covenant pushes them (the wire
    order), which is the reverse of how Levo and an explorer show them.

    The wallet rebuilds the sale's address from these terms and finds it on its
    own node before it takes anything, so a wrong field here is refused there,
    never bought."""
    why = fillable(sale)
    if why:
        raise LeafError(why)
    t = sale.terms
    f = sale.funding
    return {
        "covenant_txid": f["txid"],
        "covenant_vout": int(f["vout"]),
        "asset_a": C.asset_to_wire(t.token_asset).hex(),
        "asset_b": C.asset_to_wire(t.payment_asset).hex(),
        "rate_num": str(t.price_num),
        "rate_den": str(t.price_den),
        "maker_prog": _b64(bytes.fromhex(t.treasury_prog)),
        "maker_prog_ver": 1,
        "min_lot": str(t.min_lot),
        "expiry_locktime": int(t.close_locktime),
        "maker_x": _b64(bytes.fromhex(t.reclaim_xonly)),
        "internal_key": _b64(K.NUMS),
        "merkle_path": [_b64(K.tapleaf_hash(sale.cov.reclaim_leaf))],
    }


def read_take(st, sale):
    """What a take's status says about this sale, checked against the sale's own
    terms, or LeafError for a take that is not a purchase from it.

    Returns {state, order: {txid, vout}, token_atoms, payment_atoms, locked,
    round, input, round_filled, reason}."""
    if not isinstance(st, dict):
        raise LeafError("the operator's answer is not a take's status")
    f = st.get("fill")
    if not isinstance(f, dict):
        raise LeafError("that participation is not a take of an on-chain order")
    t = sale.terms
    if str(f.get("script") or "").lower() != sale.script_pubkey.lower():
        raise LeafError("that take is of another order, not of this sale: its order's script is not this sale's address")
    if str(f.get("asset_a") or "").lower() != t.token_asset or str(f.get("asset_b") or "").lower() != t.payment_asset:
        raise LeafError("that take buys %s with %s, and this sale sells %s for %s"
                        % (f.get("asset_a"), f.get("asset_b"), t.token_asset, t.payment_asset))
    try:
        filled = int(f["filled"])
        price = int(f["price"])
        locked = int(f["locked"])
    except (KeyError, TypeError, ValueError):
        raise LeafError("the take's status names no fill")
    try:
        cost = t.cost_for(filled)
    except ValueError as e:
        raise LeafError("the take fills %d atoms: %s" % (filled, e))
    if price != cost:
        raise LeafError("the take prices its fill at %d, and the covenant at %d" % (price, cost))
    txid, _, vout = str(f.get("order") or "").partition(":")
    if not re.match(r"^[0-9a-f]{64}$", txid) or not vout.isdigit():
        raise LeafError("the take names no order outpoint")
    rnd = st.get("round") or {}
    state = str(st.get("state") or "")
    out = {"state": state, "order": {"txid": txid, "vout": int(vout)}, "token_atoms": filled,
           "payment_atoms": price, "locked": locked, "round": None, "input": None, "round_filled": None,
           "reason": st.get("void_reason") or st.get("waiting")}
    if rnd.get("txid") and f.get("input") is not None:
        out["round"] = str(rnd["txid"]).lower()
        out["input"] = int(f["input"])
        if f.get("round_filled") is not None:
            out["round_filled"] = int(f["round_filled"])
    return out


def check_round(raw, sale, take, shares):
    """Whether the decoded transaction `raw` is the round the operator says it
    is, for this sale: it spends the take's order at input k, pays this sale's
    treasury at 2k at least the covenant's price of the round's fill, rests any
    remainder at this sale's address at 2k+1, and fills at least `shares`, the
    shares Levo records of it together. Returns the round's fill, or raises
    LeafError saying what does not hold."""
    k = int(take["input"])
    vin = raw.get("vin") or []
    vout = raw.get("vout") or []
    if len(vin) <= k or len(vout) <= 2 * k:
        raise LeafError("the round has no input %d or output %d" % (k, 2 * k))
    o = take["order"]
    if vin[k].get("txid") != o["txid"] or int(vin[k].get("vout", -1)) != int(o["vout"]):
        raise LeafError("input %d of the round is not the sale at %s:%s" % (k, o["txid"], o["vout"]))
    rem = 0
    if len(vout) > 2 * k + 1:
        r = vout[2 * k + 1]
        spk = ((r.get("scriptPubKey") or {}).get("hex") or "").lower()
        if spk == sale.script_pubkey.lower() and (r.get("asset") or "").lower() == sale.terms.token_asset:
            rem = _atoms(r)
    filled = int(take["locked"]) - rem
    if take.get("round_filled") is not None and filled != int(take["round_filled"]):
        raise LeafError("the round fills %d of the sale and the operator says %d" % (filled, take["round_filled"]))
    if filled < shares:
        raise LeafError("the round fills %d of the sale, less than the %d of the takes Levo records in it" % (filled, shares))
    credit = vout[2 * k]
    spk = ((credit.get("scriptPubKey") or {}).get("hex") or "").lower()
    if spk != sale.terms.treasury_spk.hex() or (credit.get("asset") or "").lower() != sale.terms.payment_asset:
        raise LeafError("output %d of the round does not pay this sale's treasury in its payment asset" % (2 * k))
    if _atoms(credit) < sale.terms.cost_for(filled):
        raise LeafError("output %d pays the treasury %d, less than the price of the round's fill" % (2 * k, _atoms(credit)))
    return filled


def _atoms(out):
    if out.get("valueatoms") is not None:
        return int(out["valueatoms"])
    if out.get("amountatoms") is not None:
        return int(out["amountatoms"])
    v = out.get("value")
    if v is None:
        return 0
    from decimal import Decimal
    return int((Decimal(str(v)) * 100_000_000).to_integral_value())
