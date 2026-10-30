"""The Lightning sale: a sale priced in BTC and paid over Lightning.

A Sequentia covenant cannot read a Bitcoin output, so it cannot check that a
buyer paid in BTC. A hash lock does not need to. A Lightning payment reveals a
secret (the preimage of its payment hash) to the payer at the moment it
completes, and a script can check that secret. So the tokens are put where
only that secret can deliver them, and the payment is what hands the buyer the
secret:

  1. The buyer reserves a lot. The project's receiver -- the service that runs
     the project's Lightning node -- issues an invoice and signs a transaction
     that moves the lot out of the sale into a LOT LOCK under the invoice's
     payment hash, with the buyer's destination written into the lock. The
     buyer adds a small BOND under the same hash, against reserving without
     paying.
  2. The buyer pays the invoice in BTC, from any Lightning wallet or a Bitcoin
     Ark balance, and learns the secret.
  3. The secret delivers the lot to the destination written in the lock, and,
     with the buyer's signature, returns the bond.

If the invoice is never paid, the lot re-rests at the sale address once the
lock's timeout has passed, and the project keeps the bond.

Four scripts carry this, all taproot outputs with the NUMS internal key, so
none of them has a key path:

  SALE      the sale address. RESERVE: the receiver's signature moves a lot to
            output 2k and re-rests the remainder at output 2k+1, at this very
            address. SELL (when the sale also settles in a Sequentia asset):
            the permissionless leaf of covenant.py, unchanged. RECLAIM: the
            project, from the close on.
  LOT LOCK  CLAIM: the payment's secret, and the output at the input's own
            index pays the lot, all of it, to the destination the lock names.
            No signature: whoever holds the secret can only deliver the lot to
            the buyer. RE-REST: after the timeout, the output at the input's
            own index pays the lot, all of it, back to the sale address. No
            signature either, so an unpaid lot needs nobody to come back.
  BOND LOCK REFUND: the secret and the buyer's signature. FORFEIT: after the
            timeout, the receiver's signature.
  VESTING   (when the sale vests) the destination a lot lock names: the
            buyer's key, from the vesting time on.

What each party can and cannot do, all of it consensus:

  * Nobody takes a lot without the secret, and the secret can only send it to
    the buyer's destination.
  * Before the timeout a lot cannot go back to the sale, and the project
    cannot take the bond; the bond goes back to the buyer with the secret.
  * The receiver's key moves lots out of the sale. The project holds the
    secret of every invoice it issues, so it can always take its own lots, the
    same way a project can always buy its own sale out at the published price.
    That key is the project's, like the treasury; keep it like one.

The invoice amount is the project's: the chain cannot see a Bitcoin price, so
the buyer's wallet checks the invoice against the published BTC price before
paying. And the buyer must claim before the timeout: the receiver can hold a
Lightning payment until its expiry, so a wallet pays only while the time left
on the lock is longer than the longest the payment can be held, with a margin
to claim in.
"""

import hashlib

import covenant as C
import script as K
import secp256k1 as EC
from script import Op, NUMS

OP_SIZE = 0x82
OP_SHA256 = 0xa8
OP_CHECKSIGVERIFY = 0xad
OP_CHECKSEQUENCEVERIFY = 0xb2

# BIP68: a relative lock in units of 512 seconds is flagged by bit 22. Only
# time-based locks: a block count would stop meaning anything the moment the
# chain's block spacing changes, and the anchoring rule makes time the measure.
SEQUENCE_TYPE_FLAG = 1 << 22
SEQUENCE_UNIT_SECONDS = 512
MAX_TIMEOUT_UNITS = 0xffff


def timeout_units(seconds):
    """A relative timeout in 512-second units, rounded up."""
    s = int(seconds)
    if s < 1:
        raise ValueError("the lock's timeout must be more than zero")
    u = -(-s // SEQUENCE_UNIT_SECONDS)
    if u > MAX_TIMEOUT_UNITS:
        raise ValueError("the lock's timeout is at most %d seconds" % (MAX_TIMEOUT_UNITS * 512))
    return u


def csv_value(units):
    """The number a time-based OP_CHECKSEQUENCEVERIFY checks, and the nSequence
    a spend through it must carry."""
    return SEQUENCE_TYPE_FLAG | int(units)


def _O(*v):
    return K.ops(*v)


def _here():
    return _O(K.OP_PUSHCURRENTINPUTINDEX)


def _pin_output_here(spk, token_wire):
    """The output at this input's own index pays exactly this input's value of
    `token_wire`, explicitly, to `spk` (a v0 or v1 witness output)."""
    ver, prog = _witness(spk)
    s = []
    s += _here() + _O(K.OP_INSPECTOUTPUTSCRIPTPUBKEY, K.OP_1 if ver == 1 else K.OP_0,
                      K.OP_EQUALVERIFY) + [prog] + _O(K.OP_EQUALVERIFY)
    s += _here() + _O(K.OP_INSPECTOUTPUTASSET, K.OP_1, K.OP_EQUALVERIFY) + [token_wire] \
        + _O(K.OP_EQUALVERIFY)
    s += _here() + _O(K.OP_INSPECTOUTPUTVALUE, K.OP_1, K.OP_EQUALVERIFY)
    s += _here() + _O(K.OP_INSPECTINPUTVALUE, K.OP_1, K.OP_EQUALVERIFY)
    s += _O(K.OP_EQUAL)
    return s


def _witness(spk):
    spk = bytes(spk)
    if len(spk) == 34 and spk[0] == 0x51 and spk[1] == 0x20:
        return 1, spk[2:]
    if len(spk) in (22, 34) and spk[0] == 0x00 and spk[1] == len(spk) - 2:
        return 0, spk[2:]
    raise ValueError("a destination must be a version-0 or taproot witness output")


def _secret_check(payment_hash):
    if len(payment_hash) != 32:
        raise ValueError("a payment hash is 32 bytes")
    # The size check keeps the secret to the 32 bytes Lightning uses.
    return _O(OP_SIZE) + [32] + _O(K.OP_EQUALVERIFY, OP_SHA256) + [payment_hash] \
        + _O(K.OP_EQUALVERIFY)


# --- the scripts ------------------------------------------------------------

def build_reserve_leaf(token_wire, receiver_xonly, min_lot):
    """RESERVE: the receiver's signature moves a lot of at least `min_lot` to
    output 2k, explicitly, and re-rests any remainder of at least `min_lot` at
    output 2k+1 at this very address."""
    if len(receiver_xonly) != 32:
        raise ValueError("the receiver key is a 32-byte x-only key")
    rem = C._rem_idx()
    cred = C._credit_idx()
    s = [receiver_xonly] + _O(OP_CHECKSIGVERIFY)
    s += _here() + _O(K.OP_INSPECTINPUTVALUE, K.OP_1, K.OP_EQUALVERIFY)
    s += rem + _O(K.OP_INSPECTNUMOUTPUTS, K.OP_LESSTHAN)
    s += _O(K.OP_IF)
    s += rem + _O(K.OP_INSPECTOUTPUTASSET, K.OP_1, K.OP_EQUALVERIFY) + [token_wire] + _O(K.OP_EQUAL)
    s += _O(K.OP_IF)
    s += rem + _O(K.OP_INSPECTOUTPUTSCRIPTPUBKEY)
    s += _here() + _O(K.OP_INSPECTINPUTSCRIPTPUBKEY)
    s += _O(K.OP_ROT, K.OP_EQUALVERIFY, K.OP_EQUALVERIFY)
    s += rem + _O(K.OP_INSPECTOUTPUTVALUE, K.OP_1, K.OP_EQUALVERIFY)
    s += _O(K.OP_DUP) + [C.le8(min_lot)] + _O(K.OP_GREATERTHANOREQUAL64, K.OP_VERIFY)
    s += _O(K.OP_ELSE) + [C.le8(0)] + _O(K.OP_ENDIF)
    s += _O(K.OP_ELSE) + [C.le8(0)] + _O(K.OP_ENDIF)
    # lot = locked - remainder, at least the minimum, paid in the token at 2k.
    s += _O(K.OP_SUB64, K.OP_VERIFY)
    s += _O(K.OP_DUP) + [C.le8(min_lot)] + _O(K.OP_GREATERTHANOREQUAL64, K.OP_VERIFY)
    s += cred + _O(K.OP_INSPECTOUTPUTASSET, K.OP_1, K.OP_EQUALVERIFY) + [token_wire] \
        + _O(K.OP_EQUALVERIFY)
    s += cred + _O(K.OP_INSPECTOUTPUTVALUE, K.OP_1, K.OP_EQUALVERIFY)
    s += _O(K.OP_EQUAL)
    return K.script(s)


def build_claim_leaf(payment_hash, dest_spk, token_wire):
    return K.script(_secret_check(payment_hash) + _pin_output_here(dest_spk, token_wire))


def build_rerest_leaf(units, sale_spk, token_wire):
    return K.script([csv_value(units)] + _O(OP_CHECKSEQUENCEVERIFY, K.OP_DROP)
                    + _pin_output_here(sale_spk, token_wire))


def build_refund_leaf(payment_hash, buyer_xonly):
    if len(buyer_xonly) != 32:
        raise ValueError("the buyer key is a 32-byte x-only key")
    return K.script(_secret_check(payment_hash) + [buyer_xonly] + _O(K.OP_CHECKSIG))


def build_forfeit_leaf(units, receiver_xonly):
    return K.script([csv_value(units)] + _O(OP_CHECKSEQUENCEVERIFY, K.OP_DROP)
                    + [receiver_xonly] + _O(K.OP_CHECKSIG))


def build_vest_leaf(vest_locktime, buyer_xonly):
    return K.script([int(vest_locktime)] + _O(K.OP_CHECKLOCKTIMEVERIFY, K.OP_DROP)
                    + [buyer_xonly] + _O(K.OP_CHECKSIG))


class Tree:
    """A taproot tree of one, two or three leaves over NUMS. Three leaves sit
    as ((a, b), c)."""

    def __init__(self, leaves):
        if not 1 <= len(leaves) <= 3:
            raise ValueError("one to three leaves")
        self.internal_key = NUMS
        self.leaves = dict(leaves)
        names = [n for n, _ in leaves]
        h = {n: K.tapleaf_hash(s) for n, s in leaves}
        self.path = {}
        if len(names) == 1:
            root = h[names[0]]
            self.path[names[0]] = b""
        elif len(names) == 2:
            a, b = names
            root = K.tapbranch_hash(h[a], h[b])
            self.path = {a: h[b], b: h[a]}
        else:
            a, b, c = names
            ab = K.tapbranch_hash(h[a], h[b])
            root = K.tapbranch_hash(ab, h[c])
            self.path = {a: h[b] + h[c], b: h[a] + h[c], c: ab}
        self.merkle_root = root
        self.output_key, neg = EC.tweak_add_pubkey(
            NUMS, EC.tagged_hash("TapTweak/elements", NUMS + root))
        self.negflag = 1 if neg else 0
        self.script_pubkey = bytes([K.OP_1, 0x20]) + self.output_key

    def control_block(self, name):
        return bytes([K.LEAF_VERSION_TAPSCRIPT + self.negflag]) + NUMS + self.path[name]

    def witness(self, name, *stack):
        return list(stack) + [self.leaves[name], self.control_block(name)]


def _b32(v, what):
    b = bytes.fromhex(v) if isinstance(v, str) else bytes(v)
    if len(b) != 32:
        raise ValueError("%s must be 32 bytes" % what)
    return b


def _xonly(v, what):
    b = _b32(v, what)
    if EC.lift_x(int.from_bytes(b, "big")) is None:
        raise ValueError("%s is not an x-only public key" % what)
    return b


class LightningSale:
    """The sale address of a sale paid over Lightning.

    `sell_terms` (a covenant.SaleTerms) adds the permissionless SELL leaf, so
    the same address also settles directly in a Sequentia asset. Its token,
    minimum and close must match.
    """

    def __init__(self, token_asset, receiver_xonly, min_lot, close_locktime, reclaim_xonly,
                 sell_terms=None):
        self.token_asset = C._hex32(token_asset, "token_asset")
        self.receiver = _xonly(receiver_xonly, "the receiver key")
        self.min_lot = int(min_lot)
        if self.min_lot < 1:
            raise ValueError("the minimum purchase must be at least one atom")
        self.close_locktime = int(close_locktime)
        self.reclaim = _xonly(reclaim_xonly, "the reclaim key")
        tw = C.asset_to_wire(self.token_asset)
        self.reserve_leaf = build_reserve_leaf(tw, self.receiver, self.min_lot)
        self.reclaim_leaf = C.build_reclaim_leaf(self.close_locktime, self.reclaim)
        leaves = [("reserve", self.reserve_leaf)]
        self.sell_terms = sell_terms
        if sell_terms is not None:
            if (sell_terms.token_asset != self.token_asset or sell_terms.min_lot != self.min_lot
                    or sell_terms.close_locktime != self.close_locktime
                    or sell_terms.reclaim_xonly != self.reclaim.hex()):
                raise ValueError("the direct-settlement terms must share the token, the "
                                 "minimum, the close and the reclaim key")
            self.sell_leaf = C.build_sell_leaf(
                tw, C.asset_to_wire(sell_terms.payment_asset), sell_terms.price_num,
                sell_terms.price_den, bytes.fromhex(sell_terms.treasury_prog),
                sell_terms.min_lot, treasury_ver=sell_terms.treasury_ver)
            leaves.append(("sell", self.sell_leaf))
        leaves.append(("reclaim", self.reclaim_leaf))
        self.tree = Tree(leaves)
        self.script_pubkey = self.tree.script_pubkey

    @property
    def spk_hex(self):
        return self.script_pubkey.hex()


class LotLock:
    """One reservation: the lot lock, the bond lock, and where the lot goes."""

    def __init__(self, sale, payment_hash, buyer_xonly, dest_spk, timeout_seconds):
        self.sale = sale
        self.payment_hash = _b32(payment_hash, "the payment hash")
        self.buyer = _xonly(buyer_xonly, "the buyer key")
        self.dest_spk = bytes.fromhex(dest_spk) if isinstance(dest_spk, str) else bytes(dest_spk)
        _witness(self.dest_spk)
        self.units = timeout_units(timeout_seconds)
        tw = C.asset_to_wire(sale.token_asset)
        self.claim_leaf = build_claim_leaf(self.payment_hash, self.dest_spk, tw)
        self.rerest_leaf = build_rerest_leaf(self.units, sale.script_pubkey, tw)
        self.lot = Tree([("claim", self.claim_leaf), ("rerest", self.rerest_leaf)])
        self.refund_leaf = build_refund_leaf(self.payment_hash, self.buyer)
        self.forfeit_leaf = build_forfeit_leaf(self.units, sale.receiver)
        self.bond = Tree([("refund", self.refund_leaf), ("forfeit", self.forfeit_leaf)])

    @property
    def sequence(self):
        return csv_value(self.units)

    @property
    def timeout_seconds(self):
        return self.units * SEQUENCE_UNIT_SECONDS


class Vesting:
    """A destination that pays the buyer's key from `vest_locktime` on."""

    def __init__(self, vest_locktime, buyer_xonly):
        self.locktime = int(vest_locktime)
        if not 0 < self.locktime <= 0xffffffff:
            raise ValueError("the vesting time is a block height or a unix time")
        self.buyer = _xonly(buyer_xonly, "the buyer key")
        self.leaf = build_vest_leaf(self.locktime, self.buyer)
        self.tree = Tree([("vest", self.leaf)])
        self.script_pubkey = self.tree.script_pubkey


def preimage_hash(preimage):
    return hashlib.sha256(bytes(preimage)).digest()
