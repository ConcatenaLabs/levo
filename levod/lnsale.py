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
  3. The secret delivers the lot to the destination written in the lock and
     returns the bond, less the claim's fee, to the buyer. Neither needs a
     signature, so whoever holds the secret -- the buyer, or the receiver once
     paid -- can broadcast the claim, and it can only pay the buyer.

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
  BOND LOCK REFUND: the secret, and the output at the input's own index
            returns the bond, less at most a fixed fee allowance, to the
            buyer's refund address. No signature: the bond pays the claim's
            fee, so a buyer needs no key and no other coin to collect.
            FORFEIT: after the timeout, the receiver's signature.
  VESTING   (when the sale vests) the destination a lot lock names: the
            buyer's key, from the vesting time on.

What each party can and cannot do, all of it consensus:

  * Nobody takes a lot without the secret, and the secret can only send it to
    the buyer's destination.
  * Before the timeout a lot cannot go back to the sale, and the project
    cannot take the bond; with the secret the bond can only go back to the
    buyer, less at most the fee allowance.
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


def build_refund_leaf(payment_hash, refund_spk, bond_wire, fee_allowance):
    """REFUND: the secret, and the output at this input's own index returns
    the bond to `refund_spk`, explicitly, in the bond's asset, worth at least
    this input's value less `fee_allowance`."""
    ver, prog = _witness(refund_spk)
    s = _secret_check(payment_hash)
    s += _here() + _O(K.OP_INSPECTOUTPUTSCRIPTPUBKEY, K.OP_1 if ver == 1 else K.OP_0,
                      K.OP_EQUALVERIFY) + [prog] + _O(K.OP_EQUALVERIFY)
    s += _here() + _O(K.OP_INSPECTOUTPUTASSET, K.OP_1, K.OP_EQUALVERIFY) + [bond_wire] \
        + _O(K.OP_EQUALVERIFY)
    s += _here() + _O(K.OP_INSPECTOUTPUTVALUE, K.OP_1, K.OP_EQUALVERIFY)
    s += [C.le8(fee_allowance)] + _O(K.OP_ADD64, K.OP_VERIFY)
    s += _here() + _O(K.OP_INSPECTINPUTVALUE, K.OP_1, K.OP_EQUALVERIFY)
    s += _O(K.OP_GREATERTHANOREQUAL64)
    return K.script(s)


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
    """One reservation: the lot lock, the bond lock, and where each goes.

    `bond_asset` and `fee_allowance` (atoms of it) shape the REFUND leaf: the
    bond comes back to `refund_spk` less at most the allowance, which pays the
    claim's fee.
    """

    def __init__(self, sale, payment_hash, dest_spk, refund_spk, bond_asset, fee_allowance,
                 timeout_seconds):
        self.sale = sale
        self.payment_hash = _b32(payment_hash, "the payment hash")
        self.dest_spk = _spk(dest_spk)
        self.refund_spk = _spk(refund_spk)
        self.bond_asset = C._hex32(bond_asset, "the bond asset")
        self.fee_allowance = int(fee_allowance)
        if not 0 <= self.fee_allowance < (1 << 62):
            raise ValueError("the fee allowance is a whole number of atoms")
        self.units = timeout_units(timeout_seconds)
        tw = C.asset_to_wire(sale.token_asset)
        self.claim_leaf = build_claim_leaf(self.payment_hash, self.dest_spk, tw)
        self.rerest_leaf = build_rerest_leaf(self.units, sale.script_pubkey, tw)
        self.lot = Tree([("claim", self.claim_leaf), ("rerest", self.rerest_leaf)])
        self.refund_leaf = build_refund_leaf(self.payment_hash, self.refund_spk,
                                             C.asset_to_wire(self.bond_asset), self.fee_allowance)
        self.forfeit_leaf = build_forfeit_leaf(self.units, sale.receiver)
        self.bond = Tree([("refund", self.refund_leaf), ("forfeit", self.forfeit_leaf)])

    @property
    def sequence(self):
        return csv_value(self.units)

    @property
    def timeout_seconds(self):
        return self.units * SEQUENCE_UNIT_SECONDS


def _spk(v):
    b = bytes.fromhex(v) if isinstance(v, str) else bytes(v)
    _witness(b)
    return b


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


# --- a sale's Lightning terms, and Levo's view of the address ----------------

DEFAULT_TIMEOUT = 6 * 3600


class LightningTerms:
    """What a sale's Lightning half publishes.

    Only the receiver key is in the address (the RESERVE leaf). The rest is
    what the receiver and Levo build each reservation from, and what a buyer's
    wallet checks a lock against: the BTC price (`btc_num`/`btc_den`
    satoshis per token atom, rounded up per lot), where the receiver answers,
    the lock's timeout, the bond, the fee the bond may pay on its way back,
    and a vesting time when the tokens vest.
    """

    def __init__(self, receiver_xonly, receiver_url, btc_num, btc_den, bond_asset, bond_atoms,
                 fee_allowance, timeout_seconds=DEFAULT_TIMEOUT, vest_locktime=0):
        self.receiver_xonly = _xonly(receiver_xonly, "the receiver key").hex()
        self.receiver_url = str(receiver_url or "").strip().rstrip("/")
        if not self.receiver_url.startswith(("http://", "https://")):
            raise ValueError("the receiver's address must be an http(s) URL")
        self.btc_num, self.btc_den = C.canonical_price(btc_num, btc_den)
        self.bond_asset = C._hex32(bond_asset, "bond_asset")
        self.bond_atoms = C._int(bond_atoms, "bond_atoms")
        self.fee_allowance = C._int(fee_allowance, "fee_allowance")
        if self.bond_atoms <= self.fee_allowance:
            raise ValueError("the bond must be larger than the fee it may pay on its way back")
        self.timeout_seconds = timeout_units(timeout_seconds) * SEQUENCE_UNIT_SECONDS
        self.vest_locktime = C._int(vest_locktime or 0, "vest_locktime")
        if not 0 <= self.vest_locktime <= 0xffffffff:
            raise ValueError("the vesting time is a block height or a unix time")

    def sats_for(self, token_atoms):
        return -(-int(token_atoms) * self.btc_num // self.btc_den)

    def to_json(self):
        return {"receiver_xonly": self.receiver_xonly, "receiver_url": self.receiver_url,
                "btc_num": self.btc_num, "btc_den": self.btc_den,
                "bond_asset": self.bond_asset, "bond_atoms": C.atoms_out(self.bond_atoms),
                "fee_allowance": C.atoms_out(self.fee_allowance),
                "timeout_seconds": self.timeout_seconds, "vest_locktime": self.vest_locktime}

    @classmethod
    def from_json(cls, d):
        if isinstance(d, LightningTerms):
            return d
        if not isinstance(d, dict):
            raise ValueError("the Lightning terms must be an object")
        for k in ("receiver_xonly", "receiver_url", "btc_num", "btc_den", "bond_asset",
                  "bond_atoms", "fee_allowance"):
            if d.get(k) in (None, ""):
                raise ValueError("the Lightning terms are missing %s" % k)
        return cls(d["receiver_xonly"], d["receiver_url"], d["btc_num"], d["btc_den"],
                   d["bond_asset"], d["bond_atoms"], d["fee_allowance"],
                   d.get("timeout_seconds") or DEFAULT_TIMEOUT, d.get("vest_locktime") or 0)


class LightningCovenant:
    """A Lightning sale as the rest of Levo sees a sale: the ordinary SELL and
    RECLAIM leaves (so a purchase in the sale's asset and the reclaim work as
    for any sale) beside RESERVE."""

    capped = False

    def __init__(self, terms):
        self.terms = terms
        ln = terms.lightning
        self.sale = LightningSale(terms.token_asset, ln.receiver_xonly, terms.min_lot,
                                  terms.close_locktime, terms.reclaim_xonly, sell_terms=terms)
        self.sell_leaf = self.sale.sell_leaf
        self.reclaim_leaf = self.sale.reclaim_leaf
        self.reserve_leaf = self.sale.reserve_leaf
        self.tap = self.sale.tree
        self.script_pubkey = self.sale.script_pubkey

    @property
    def spk_hex(self):
        return self.script_pubkey.hex()

    def sell_witness(self):
        return self.tap.witness("sell")

    def reclaim_witness(self, project_sig):
        return self.tap.witness("reclaim", project_sig)

    def reserve_witness(self, receiver_sig):
        return self.tap.witness("reserve", receiver_sig)

    def verify_funding(self, onchain_spk):
        if isinstance(onchain_spk, str):
            onchain_spk = bytes.fromhex(onchain_spk)
        if bytes(onchain_spk) != self.script_pubkey:
            raise ValueError("funded scriptPubKey %s does not match the sale terms, which derive %s"
                             % (bytes(onchain_spk).hex(), self.spk_hex))
        return True

    def lock(self, payment_hash, dest_spk, refund_spk):
        ln = self.terms.lightning
        return LotLock(self.sale, payment_hash, dest_spk, refund_spk, ln.bond_asset,
                       ln.fee_allowance, ln.timeout_seconds)

    def destination(self, buyer_spk, buyer_xonly=None):
        """Where a lot goes: the buyer's address, or, when the sale vests, the
        vesting script for the buyer's key."""
        vest = self.terms.lightning.vest_locktime
        if not vest:
            return bytes.fromhex(buyer_spk) if isinstance(buyer_spk, str) else bytes(buyer_spk)
        if not buyer_xonly:
            raise ValueError("this sale vests, so a lot is locked to the buyer's key: name it")
        return Vesting(vest, buyer_xonly).script_pubkey


# --- the transactions --------------------------------------------------------

def build_reserve(cov, sale_coin, token_atoms, lock, buyer_inputs, change_spk, fee_atoms):
    """The reservation, unsigned: the sale coin at input 0, the buyer's coins
    after it. Output 0 is the lot lock, output 1 the remainder (when any),
    then the bond lock, the buyer's change in each asset, and the fee in the
    bond's asset.

    `sale_coin` is {txid, vout, atoms}; `buyer_inputs` are verified coins
    ({txid, vout, asset, value_atoms, script_pubkey}). Returns the transaction
    the outputs it spends, in input order, as (asset, atoms, spk), and the
    bond lock's output index.
    """
    import tx as TX
    ln = cov.terms.lightning
    token = cov.terms.token_asset
    left = int(sale_coin["atoms"]) - int(token_atoms)
    if int(token_atoms) < cov.terms.min_lot:
        raise ValueError("a lot is at least the sale's minimum")
    if left < 0:
        raise ValueError("the sale holds fewer tokens than that")
    if 0 < left < cov.terms.min_lot:
        raise ValueError("that would leave less than the minimum in the sale; take it all")
    t = TX.Transaction()
    t.vin.append(TX.TxIn(sale_coin["txid"], sale_coin["vout"]))
    spent = [(token, int(sale_coin["atoms"]), cov.script_pubkey)]
    have = {}
    for c in buyer_inputs:
        t.vin.append(TX.TxIn(c["txid"], c["vout"]))
        spent.append((c["asset"], int(c["value_atoms"]), bytes.fromhex(c["script_pubkey"])))
        have[c["asset"]] = have.get(c["asset"], 0) + int(c["value_atoms"])
    t.vout.append(TX.TxOut(token, int(token_atoms), lock.lot.script_pubkey))
    if left:
        t.vout.append(TX.TxOut(token, left, cov.script_pubkey))
    bond_vout = len(t.vout)
    t.vout.append(TX.TxOut(ln.bond_asset, ln.bond_atoms, lock.bond.script_pubkey))
    need = {ln.bond_asset: ln.bond_atoms + int(fee_atoms)}
    for asset, n in need.items():
        if have.get(asset, 0) < n:
            raise ValueError("the coins given hold %d atoms of the bond's asset; the bond and the "
                             "fee need %d" % (have.get(asset, 0), n))
    for asset, n in sorted(have.items()):
        back = n - need.get(asset, 0)
        if back:
            t.vout.append(TX.TxOut(asset, back, change_spk))
    t.vout.append(TX.TxOut(ln.bond_asset, int(fee_atoms), b""))
    return t, spent, bond_vout


def build_claim(lock, reserve_txid, lot_vout, lot_atoms, bond_vout, bond_atoms, preimage, fee_atoms):
    """The claim, complete: no signature. The lot goes to the destination and
    the bond, less the fee, to the refund address."""
    import tx as TX
    if hashlib.sha256(bytes(preimage)).digest() != lock.payment_hash:
        raise ValueError("that secret is not the payment hash's")
    if int(fee_atoms) > lock.fee_allowance:
        raise ValueError("the fee is more than the bond may pay")
    t = TX.Transaction()
    t.vin.append(TX.TxIn(reserve_txid, lot_vout))
    t.vin.append(TX.TxIn(reserve_txid, bond_vout))
    t.vout.append(TX.TxOut(lock.sale.token_asset, int(lot_atoms), lock.dest_spk))
    t.vout.append(TX.TxOut(lock.bond_asset, int(bond_atoms) - int(fee_atoms), lock.refund_spk))
    t.vout.append(TX.TxOut(lock.bond_asset, int(fee_atoms), b""))
    t.vin[0].witness = lock.lot.witness("claim", bytes(preimage))
    t.vin[1].witness = lock.bond.witness("refund", bytes(preimage))
    return t


def build_rerest(lock, reserve_txid, lot_vout, lot_atoms, bond_vout, bond_atoms, project_spk,
                 fee_atoms):
    """The re-rest after the timeout, unsigned: the lot back to the sale, the
    bond less the fee to the project. Input 1 needs the receiver's signature
    on the FORFEIT leaf (`forfeit_sighash`)."""
    import tx as TX
    seq = lock.sequence
    t = TX.Transaction()
    t.vin.append(TX.TxIn(reserve_txid, lot_vout, sequence=seq))
    t.vin.append(TX.TxIn(reserve_txid, bond_vout, sequence=seq))
    t.vout.append(TX.TxOut(lock.sale.token_asset, int(lot_atoms), lock.sale.script_pubkey))
    t.vout.append(TX.TxOut(lock.bond_asset, int(bond_atoms) - int(fee_atoms), project_spk))
    t.vout.append(TX.TxOut(lock.bond_asset, int(fee_atoms), b""))
    spent = [(lock.sale.token_asset, int(lot_atoms), lock.lot.script_pubkey),
             (lock.bond_asset, int(bond_atoms), lock.bond.script_pubkey)]
    t.vin[0].witness = lock.lot.witness("rerest")
    return t, spent
