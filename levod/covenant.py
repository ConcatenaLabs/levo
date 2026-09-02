"""Levo sale covenants: the lock that makes a token sale trustless.

A Levo sale is a covenant. The project locks its entire sale allocation in ONE
taproot UTXO whose internal key is NUMS -- a point with no known discrete log,
so there is no key path and no way to spend the output except through one of its
two leaves:

  SELL     permissionless, and needing no signature from anyone. A buyer may
           spend the sale UTXO if and only if the spending transaction pays the
           project's treasury the agreed price in the agreed asset. Every sale
           parameter -- the token, the payment asset, the price, the treasury
           scriptPubKey, the minimum lot -- is a constant compiled into the leaf
           and therefore committed inside the taproot output key. A buyer can
           satisfy those terms; nobody can alter them. A partial buy must re-pay
           the unsold remainder to the IDENTICAL covenant, so the sale keeps
           resting until it sells out or closes.

  RECLAIM  after the close locktime, the project sweeps whatever did not sell,
           under its own signature.

Three consequences carry the whole platform, and all three are consensus rules
rather than Levo's promises:

  * Levo never holds the tokens. They are in the covenant from lock to delivery,
    and no leaf mentions Levo.
  * Payment and delivery are the same transaction. There is no state where the
    project has been paid and the buyer has not been delivered.
  * The project cannot withdraw, reprice, or redirect a live sale. NUMS is what
    makes that true; a non-NUMS internal key would be a hidden cancel, and
    `derive` refuses to build one.

The script this module emits is checked byte for byte against `vectors.json` on
every import. That check is not ceremony: a covenant differing by ONE byte
derives a different address, and a project that funded it would have locked its
tokens somewhere no one can ever spend.
"""

import json
from pathlib import Path

import script as K
from script import Op, NUMS

VECTORS_PATH = Path(__file__).with_name("vectors.json")


class CovenantDrift(RuntimeError):
    """This module no longer produces the bytes Levo was verified against."""


def asset_to_wire(display_hex):
    """Convert an asset id from the form people read to the form chains use.

    Asset ids, like txids, are DISPLAYED in the reverse of their wire byte
    order. `dumpassetlabels`, the explorer, the registry and every wallet quote
    USDX as 2a515539...b9de; the bytes that appear in a transaction output, and
    that OP_INSPECTOUTPUTASSET pushes onto the stack, are that reversed.

    Levo takes and shows the display form everywhere, because that is what a
    user can compare against an explorer, and reverses exactly here. Skipping
    this does not fail loudly: it builds a covenant priced in a DIFFERENT asset
    that nobody holds, and a sale that can never be filled.
    """
    b = bytes.fromhex(display_hex) if isinstance(display_hex, str) else bytes(display_hex)
    if len(b) != 32:
        raise ValueError("asset id must be 32 bytes")
    return b[::-1]


def canonical_price(num, den):
    """Reduce a price to lowest terms.

    ceil(n * num / den) is unchanged by dividing both sides by their common
    factor -- the two fractions are the same rational number -- but the covenant
    computes it with 64-bit arithmetic that ABORTS on overflow, and the
    intermediate it forms is `filled * num`. So a price written 25000000/100000000
    overflows on a sale a quarter the size that 1/4 handles comfortably.

    Reducing is therefore free in meaning and worth a great deal in headroom.
    It has to happen BEFORE the address is derived, because the numerator and
    denominator are baked into the covenant, and 25000000/100000000 and 1/4
    derive different addresses despite quoting the same price.
    """
    from math import gcd
    num, den = int(num), int(den)
    if num < 1 or den < 1:
        raise ValueError("price_num and price_den must both be at least 1")
    g = gcd(num, den)
    return num // g, den // g


def le8(n):
    """A 64-bit little-endian constant, the on-stack form of the OP_*64 operands."""
    if not 0 <= n < (1 << 63):
        raise ValueError("64-bit operand out of range: %d" % n)
    return int(n).to_bytes(8, "little")


# The covenant input at consensus index k credits the treasury at output 2k and
# re-rests its remainder at output 2k+1. Recomputing the indices from
# OP_PUSHCURRENTINPUTINDEX each time keeps the leaf free of per-spend state, and
# means two covenant inputs can never both point at one shared credit output --
# so a single payment can never settle two sales.
def _credit_idx():
    return K.ops(K.OP_PUSHCURRENTINPUTINDEX, K.OP_DUP, K.OP_ADD)


def _rem_idx():
    return K.ops(K.OP_PUSHCURRENTINPUTINDEX, K.OP_DUP, K.OP_ADD, K.OP_1ADD)


def build_sell_leaf(token_asset, payment_asset, price_num, price_den,
                    treasury_prog, min_lot, treasury_ver=1):
    """The permissionless SELL leaf.

    It reads the price it must be paid entirely from transaction introspection,
    so a spend supplies no witness data at all beyond the leaf and its control
    block.
    """
    if len(token_asset) != 32 or len(payment_asset) != 32:
        raise ValueError("asset ids are 32 bytes, in internal byte order")
    if price_num < 1 or price_den < 1 or min_lot < 1:
        raise ValueError("price and minimum lot must all be at least 1")
    if treasury_ver != 1:
        raise ValueError("this builder pins a v1 taproot treasury payout")
    if len(treasury_prog) != 32:
        raise ValueError("treasury witness program must be 32 bytes")

    O = K.ops
    s = []
    # locked = this covenant input's own value, and it must be explicit: a
    # blinded value the covenant cannot read is refused outright.
    s += O(K.OP_PUSHCURRENTINPUTINDEX, K.OP_INSPECTINPUTVALUE)
    s += O(K.OP_1, K.OP_EQUALVERIFY)

    # remainder = token asset re-paid to output 2k+1, or zero.
    s += _rem_idx() + O(K.OP_INSPECTNUMOUTPUTS, K.OP_LESSTHAN)
    s += O(K.OP_IF)
    #   output 2k+1 exists: is it the token asset, explicitly?
    s += _rem_idx() + O(K.OP_INSPECTOUTPUTASSET)
    s += O(K.OP_1, K.OP_EQUALVERIFY)
    s += [token_asset] + O(K.OP_EQUAL)
    s += O(K.OP_IF)
    #     yes: it is the remainder, so it must re-rest at this very covenant
    s += _rem_idx() + O(K.OP_INSPECTOUTPUTSCRIPTPUBKEY)
    s += O(K.OP_PUSHCURRENTINPUTINDEX, K.OP_INSPECTINPUTSCRIPTPUBKEY)
    s += O(K.OP_ROT, K.OP_EQUALVERIFY, K.OP_EQUALVERIFY)
    s += _rem_idx() + O(K.OP_INSPECTOUTPUTVALUE, K.OP_1, K.OP_EQUALVERIFY)
    s += O(K.OP_DUP) + [le8(min_lot)] + O(K.OP_GREATERTHANOREQUAL64, K.OP_VERIFY)
    s += O(K.OP_ELSE)
    #     no: buyer change or a receipt, so nothing was left resting
    s += [le8(0)]
    s += O(K.OP_ENDIF)
    s += O(K.OP_ELSE)
    #   output 2k+1 absent: a full buy, nothing left resting
    s += [le8(0)]
    s += O(K.OP_ENDIF)

    # filled = locked - remainder, and it must clear the minimum lot.
    s += O(K.OP_SUB64, K.OP_VERIFY)
    s += O(K.OP_DUP) + [le8(min_lot)] + O(K.OP_GREATERTHANOREQUAL64, K.OP_VERIFY)

    # required = ceil(filled * num / den), computed as floor((filled*num + den-1)/den)
    # so the rounding falls in the project's favour.
    s += [le8(price_num)] + O(K.OP_MUL64, K.OP_VERIFY)
    s += [le8(price_den - 1)] + O(K.OP_ADD64, K.OP_VERIFY)
    s += [le8(price_den)] + O(K.OP_DIV64, K.OP_VERIFY, K.OP_NIP)

    # The treasury credit at output 2k: right asset, right script, enough value.
    s += _credit_idx() + O(K.OP_INSPECTOUTPUTASSET, K.OP_1, K.OP_EQUALVERIFY) \
        + [payment_asset] + O(K.OP_EQUALVERIFY)
    s += _credit_idx() + O(K.OP_INSPECTOUTPUTSCRIPTPUBKEY, K.OP_1, K.OP_EQUALVERIFY) \
        + [treasury_prog] + O(K.OP_EQUALVERIFY)
    s += _credit_idx() + O(K.OP_INSPECTOUTPUTVALUE, K.OP_1, K.OP_EQUALVERIFY)
    s += O(K.OP_SWAP, K.OP_GREATERTHANOREQUAL64)
    return K.script(s)


def build_reclaim_leaf(close_locktime, reclaim_xonly):
    """RECLAIM: an absolute-locktime sweep by the project after the close."""
    if len(reclaim_xonly) != 32:
        raise ValueError("reclaim key must be a 32-byte x-only public key")
    return K.script([close_locktime] + K.ops(K.OP_CHECKLOCKTIMEVERIFY, K.OP_DROP)
                    + [reclaim_xonly] + K.ops(K.OP_CHECKSIG))


# --- Levo's view of a sale --------------------------------------------------

def _int(v, name):
    """A whole number, given as an int or a decimal string. Strings are
    accepted because JavaScript cannot carry an atom count above 2**53 as a
    number, and a sale's size can be larger than that."""
    if isinstance(v, bool):
        raise ValueError("%s must be a whole number" % name)
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    if isinstance(v, float) and v.is_integer():
        return int(v)
    raise ValueError("%s must be a whole number of atoms" % name)


def _hex32(v, name):
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).hex()
    v = str(v).lower()
    if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
        raise ValueError("%s must be 32 bytes of hex, got %r" % (name, v))
    return v


class SaleTerms:
    """Everything a sale commits to on chain.

    Amounts are ATOMS throughout. `price_num`/`price_den` are payment-asset
    atoms per token atom: taking `n` token atoms costs
    ceil(n * price_num / price_den). The ceiling is the leaf's own arithmetic,
    so quoting anything else would quote a purchase the chain rejects.
    """

    def __init__(self, token_asset, payment_asset, price_num, price_den,
                 treasury_prog, min_lot, close_locktime, reclaim_xonly,
                 total_atoms=None):
        self.token_asset = _hex32(token_asset, "token_asset")
        self.payment_asset = _hex32(payment_asset, "payment_asset")
        self.price_num = _int(price_num, "price_num")
        self.price_den = _int(price_den, "price_den")
        self.treasury_prog = _hex32(treasury_prog, "treasury_prog")
        self.min_lot = _int(min_lot, "min_lot")
        self.close_locktime = _int(close_locktime, "close_locktime")
        self.reclaim_xonly = _hex32(reclaim_xonly, "reclaim_xonly")
        self.total_atoms = _int(total_atoms, "total_atoms") if total_atoms is not None else None
        self._validate()

    def _validate(self):
        if self.price_num < 1 or self.price_den < 1:
            raise ValueError("price_num and price_den must both be at least 1")
        if self.min_lot < 1:
            raise ValueError("min_lot must be at least 1")
        if self.token_asset == self.payment_asset:
            raise ValueError("a sale cannot price a token in itself")
        if self.close_locktime < 1:
            raise ValueError("close_locktime must be set; a sale with no close "
                             "could never be reclaimed")
        if self.close_locktime > 0xffffffff:
            # nLockTime is 32 bits. A larger operand compiles into the reclaim
            # leaf and can never be satisfied: the tokens could be sold but
            # never taken back.
            raise ValueError("close_locktime must be a block height or a unix "
                             "time no larger than 4294967295")
        if self.total_atoms is not None:
            if self.total_atoms < self.min_lot:
                raise ValueError("total_atoms must be at least the minimum lot, "
                                 "or the sale could never be bought")
            self.assert_no_overflow(self.total_atoms)

    def assert_no_overflow(self, locked_atoms):
        """The leaf prices with OP_MUL64/OP_ADD64, which ABORT on signed 64-bit
        overflow. A sale that can overflow is one whose last buyers cannot buy,
        leaving tokens reclaimable but unsellable. Refuse it at listing time,
        while it is still free to fix.
        """
        bound = self.price_num * int(locked_atoms) + self.price_den - 1
        if bound >= (1 << 63):
            raise ValueError(
                "sale size times price overflows the covenant's 64-bit "
                "arithmetic (%d >= 2**63). Reduce the price to lowest terms "
                "(%d/%d reduces to %d/%d), lower the denomination, or split the "
                "sale." % ((bound, self.price_num, self.price_den)
                           + canonical_price(self.price_num, self.price_den)))

    def cost_for(self, token_atoms):
        n = int(token_atoms)
        if n < self.min_lot:
            raise ValueError("below the sale's minimum lot of %d atoms" % self.min_lot)
        return (n * self.price_num + self.price_den - 1) // self.price_den

    def to_json(self):
        return {
            "token_asset": self.token_asset,
            "payment_asset": self.payment_asset,
            "price_num": self.price_num,
            "price_den": self.price_den,
            "treasury_prog": self.treasury_prog,
            "min_lot": self.min_lot,
            "close_locktime": self.close_locktime,
            "reclaim_xonly": self.reclaim_xonly,
            "total_atoms": self.total_atoms,
        }

    @classmethod
    def from_json(cls, d):
        if not isinstance(d, dict):
            raise ValueError("terms must be an object")
        for k in ("token_asset", "payment_asset", "price_num", "price_den",
                  "treasury_prog", "min_lot", "close_locktime", "reclaim_xonly"):
            if d.get(k) is None:
                raise ValueError("the terms are missing %s" % k)
        return cls(d["token_asset"], d["payment_asset"], d["price_num"],
                   d["price_den"], d["treasury_prog"], d["min_lot"],
                   d["close_locktime"], d["reclaim_xonly"], d.get("total_atoms"))


class SaleCovenant:
    """A derived sale: its leaves, its address, and the check a client must run."""

    def __init__(self, terms, internal_key=NUMS):
        if internal_key != NUMS:
            raise ValueError(
                "a Levo sale must use the NUMS internal key. Any other internal "
                "key gives the project a key path, and therefore a way to spend "
                "the sale out from under its buyers.")
        self.terms = terms
        self.sell_leaf = build_sell_leaf(
            asset_to_wire(terms.token_asset), asset_to_wire(terms.payment_asset),
            terms.price_num, terms.price_den,
            bytes.fromhex(terms.treasury_prog), terms.min_lot)
        self.reclaim_leaf = build_reclaim_leaf(
            terms.close_locktime, bytes.fromhex(terms.reclaim_xonly))
        self.tap = K.Taptree(internal_key,
                             [("sell", self.sell_leaf), ("reclaim", self.reclaim_leaf)])
        self.script_pubkey = self.tap.script_pubkey

    @property
    def spk_hex(self):
        return self.script_pubkey.hex()

    def sell_witness(self):
        """A buy needs no witness data: the leaf reads the price it must be paid
        straight out of the transaction."""
        return self.tap.witness("sell")

    def reclaim_witness(self, project_sig):
        return self.tap.witness("reclaim", project_sig)

    def verify_funding(self, onchain_spk):
        """THE check. Rebuild the sale address from the published terms and
        compare it to the scriptPubKey actually funded on chain.

        Terms and address are the same fact stated twice, so this catches a
        price altered by one atom, a swapped treasury, a moved close date, or a
        different token -- each of which would otherwise be a sale that looks
        like the published one and pays somebody else. A client that skips it
        has put Levo back in a position of trust.
        """
        if isinstance(onchain_spk, str):
            onchain_spk = bytes.fromhex(onchain_spk)
        if bytes(onchain_spk) != self.script_pubkey:
            raise ValueError(
                "funded scriptPubKey %s does not match the sale terms, which "
                "derive %s" % (bytes(onchain_spk).hex(), self.spk_hex))
        if self.tap.internal_key != NUMS:
            raise ValueError("internal key is not NUMS")
        return True


def derive(terms, internal_key=NUMS):
    return SaleCovenant(terms, internal_key)


def _check_vectors():
    if not VECTORS_PATH.is_file():
        return
    vectors = json.loads(VECTORS_PATH.read_text())
    for v in vectors["cases"]:
        # Deliberately the raw builders, not SaleTerms: these vectors pin the
        # BYTES the leaves are made of, given wire-order inputs. The display-to-
        # wire convention is a separate layer with its own tests.
        sell = build_sell_leaf(bytes.fromhex(v["token"]), bytes.fromhex(v["payment"]),
                               v["rate_num"], v["rate_den"],
                               bytes.fromhex(v["treasury_prog"]), v["min_lot"])
        reclaim = build_reclaim_leaf(v["close_locktime"],
                                     bytes.fromhex(v["reclaim_x"]))
        tap = K.Taptree(NUMS, [("sell", sell), ("reclaim", reclaim)])
        got = {"sell_leaf": sell.hex(),
               "reclaim_leaf": reclaim.hex(),
               "spk": tap.script_pubkey.hex()}
        for k, want in v["expect"].items():
            if got[k] != want:
                raise CovenantDrift(
                    "covenant drift in case %r: %s is %s, golden vector says %s. "
                    "Levo will not derive sale addresses from an unverified "
                    "builder." % (v["name"], k, got[k], want))


_check_vectors()
