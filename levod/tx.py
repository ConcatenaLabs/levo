"""Building the transaction that actually moves the tokens.

A Levo buy is one Sequentia transaction. It spends the sale covenant, pays the
project's treasury, hands the buyer their tokens, and re-rests whatever is left.
This module assembles it.

Two things about that are worth stating up front, because they decide how much
trust the buyer has to extend.

**The covenant input needs no signature.** The sell leaf reads the price it must
be paid out of the transaction itself, so its witness is just the leaf script and
its control block. Nobody has to be online, and no key has to exist, for the
sale side of the spend to be valid. Only the buyer's own funding inputs need
signing, and only the buyer can sign those.

**levod never signs.** It returns an unsigned transaction. The buyer's wallet or
node signs its own inputs and broadcasts. That is why levod holding no keys is a
statement about what it can do rather than a policy it observes.

The output layout is not cosmetic. The covenant input at index k credits the
treasury at output 2k and re-rests its remainder at output 2k+1, and the leaf
decides whether a remainder exists by asking whether output 2k+1 carries the
token asset. So on a FULL buy, output 2k+1 must not be the token asset, or the
covenant would read the buyer's own tokens as a remainder and demand they sit at
the covenant address. `build_buy` enforces that.
"""

import hashlib
import struct

import covenant as C
import script as K
import secp256k1 as S

OUTPOINT_INDEX_MASK = 0x3fffffff


# --- serialisation primitives ------------------------------------------------

def compact_size(n):
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def ser_string(b):
    return compact_size(len(b)) + b


def ser_string_vector(v):
    return compact_size(len(v)) + b"".join(ser_string(x) for x in v)


def explicit_asset(asset_display_hex):
    """An unblinded asset commitment: 0x01 followed by the id in WIRE order.

    Levo carries asset ids in the form people read them, which is the reverse of
    the wire order (see covenant.asset_to_wire). Reversing here is what makes an
    output actually carry the asset the sale advertises.
    """
    return b"\x01" + C.asset_to_wire(asset_display_hex)


def explicit_value(atoms):
    """An unblinded value: 0x01 followed by the amount, big-endian.

    Big-endian, unlike almost everything else in the format. Getting this
    backwards produces a transaction that serialises cleanly and is worth a
    wildly different amount, so it is worth the explicit note.
    """
    atoms = int(atoms)
    if not 0 <= atoms < (1 << 63):
        raise ValueError("value out of range: %d" % atoms)
    return b"\x01" + atoms.to_bytes(8, "big")


class TxIn:
    def __init__(self, txid, vout, sequence=0xffffffff, witness=None):
        self.txid = txid.lower() if isinstance(txid, str) else txid.hex()
        self.vout = int(vout)
        self.sequence = int(sequence)
        self.witness = list(witness or [])
        if self.vout & ~OUTPOINT_INDEX_MASK:
            raise ValueError("output index %d collides with the issuance/pegin "
                             "flag bits" % self.vout)

    def serialize(self):
        # txids are displayed reversed; the wire wants internal order.
        return (bytes.fromhex(self.txid)[::-1]
                + struct.pack("<I", self.vout)
                + ser_string(b"")            # scriptSig: empty, this is segwit
                + struct.pack("<I", self.sequence))


class TxOut:
    def __init__(self, asset, atoms, script_pubkey):
        self.asset = asset
        self.atoms = int(atoms)
        self.script_pubkey = (bytes.fromhex(script_pubkey)
                              if isinstance(script_pubkey, str) else bytes(script_pubkey))

    def serialize(self):
        return (explicit_asset(self.asset)
                + explicit_value(self.atoms)
                + b"\x00"                    # nonce: none, this output is not blinded
                + ser_string(self.script_pubkey))

    @property
    def is_fee(self):
        return len(self.script_pubkey) == 0


class Transaction:
    def __init__(self, version=2, locktime=0):
        self.version = version
        self.locktime = locktime
        self.vin = []
        self.vout = []

    def serialize(self, with_witness=True):
        has_wit = with_witness and any(i.witness for i in self.vin)
        r = struct.pack("<i", self.version)
        r += struct.pack("<B", 1 if has_wit else 0)
        r += compact_size(len(self.vin)) + b"".join(i.serialize() for i in self.vin)
        r += compact_size(len(self.vout)) + b"".join(o.serialize() for o in self.vout)
        r += struct.pack("<I", self.locktime)
        if has_wit:
            for i in self.vin:
                r += ser_string(b"")             # issuance amount rangeproof
                r += ser_string(b"")             # inflation keys rangeproof
                r += ser_string_vector(i.witness)
                r += ser_string_vector([])       # pegin witness
            for _ in self.vout:
                r += ser_string(b"")             # surjection proof
                r += ser_string(b"")             # range proof
        return r

    def hex(self):
        return self.serialize().hex()

    def txid(self):
        h = hashlib.sha256(hashlib.sha256(self.serialize(with_witness=False)).digest()).digest()
        return h[::-1].hex()


# --- witness programs --------------------------------------------------------

def v1_script_pubkey(program_hex):
    """OP_1 <32-byte program>: the shape both the treasury credit and the sale
    address take."""
    p = bytes.fromhex(program_hex) if isinstance(program_hex, str) else bytes(program_hex)
    if len(p) != 32:
        raise ValueError("a v1 witness program is 32 bytes")
    return bytes([K.OP_1, 0x20]) + p


class BuildError(ValueError):
    pass


# --- the buy transaction -----------------------------------------------------

def build_buy(sale, plan, buyer):
    """Assemble the unsigned buy transaction.

    `buyer` supplies the funding side, because levod has no wallet and cannot
    choose coins:

        token_script_pubkey  where the purchased tokens go
        change_script_pubkey where leftover payment asset goes (optional)
        inputs               [{txid, vout, asset, value_atoms}] the buyer will sign
        fee_atoms            the network fee
        fee_asset            asset the fee is paid in (default: the payment asset)

    Returns the transaction, its txid, and the per-input signing notes. Every
    check here has a way to lose money if skipped, so the builder refuses rather
    than producing something that merely looks right.
    """
    terms = sale.terms
    funding = sale.funding
    if not funding:
        raise BuildError("this sale is not funded, so there is nothing to spend")

    token_spk = _need_spk(buyer.get("token_script_pubkey"), "token_script_pubkey")
    change_spk = buyer.get("change_script_pubkey")
    change_spk = _need_spk(change_spk, "change_script_pubkey") if change_spk else None
    fee_asset = (buyer.get("fee_asset") or terms.payment_asset).lower()
    fee_atoms = int(buyer.get("fee_atoms") or 0)
    if fee_atoms < 0:
        raise BuildError("fee cannot be negative")

    ins = list(buyer.get("inputs") or [])
    if not ins:
        raise BuildError("supply the inputs that will pay for this purchase")

    # Confidential inputs cannot fund a covenant buy.
    #
    # A blinded output commits to its value rather than stating it, so a
    # transaction spending one only balances if the outputs are blinded too and
    # the blinding factors line up. The covenant's outputs must be EXPLICIT --
    # the sell leaf reads them with introspection and refuses anything it cannot
    # read -- so there is nothing to balance a blinded input against, and levod
    # holds no blinding keys to do it with.
    #
    # Without this check the transaction builds cleanly, the buyer signs it, and
    # the chain rejects it with `bad-txns-in-ne-out`, which says nothing about
    # what went wrong. Found the hard way against a live node.
    blinded = [i for i in ins if i.get("blinded")]
    if blinded:
        raise BuildError(
            "%d of your inputs are confidential, and a sale covenant can only "
            "be filled with explicit ones: the covenant reads the treasury "
            "payment off the transaction, so every amount in it has to be "
            "stated rather than committed. Send the funds to an unblinded "
            "address first (a tb1... address, not a tsqb1... one) and spend "
            "that output instead." % len(blinded))

    # What the buyer is bringing, per asset.
    supplied = {}
    for i in ins:
        a = str(i["asset"]).lower()
        supplied[a] = supplied.get(a, 0) + int(i["value_atoms"])

    need = {terms.payment_asset: plan.payment_atoms}
    need[fee_asset] = need.get(fee_asset, 0) + fee_atoms
    for asset, amount in need.items():
        if supplied.get(asset, 0) < amount:
            raise BuildError(
                "inputs bring %d atoms of %s but the purchase needs %d"
                % (supplied.get(asset, 0), asset, amount))
    for asset in supplied:
        if asset != terms.payment_asset and asset != fee_asset:
            raise BuildError(
                "input of asset %s is neither the payment asset nor the fee "
                "asset; it would be burned" % asset)

    tx = Transaction(version=2, locktime=0)

    # Input 0 is the covenant. Its index decides the output map, and the leaf's
    # arithmetic here assumes k = 0, so it must be first.
    tx.vin.append(TxIn(funding["txid"], funding["vout"],
                       witness=sale.cov.sell_witness()))
    for i in ins:
        tx.vin.append(TxIn(i["txid"], i["vout"]))

    # What the buyer gets back on each asset, once the purchase and the fee are
    # taken out of what they brought.
    change = {}
    for asset, brought in supplied.items():
        left = brought
        if asset == terms.payment_asset:
            left -= plan.payment_atoms
        if asset == fee_asset:
            left -= fee_atoms
        if left < 0:
            raise BuildError("inputs do not cover %s" % asset)
        if left:
            change[asset] = left
    if change and not change_spk:
        raise BuildError(
            "this purchase leaves change (%s) but no change_script_pubkey was "
            "given; without one that value would be burned"
            % ", ".join("%d atoms of %s" % (v, a) for a, v in sorted(change.items())))

    fee_out = TxOut(fee_asset, fee_atoms, b"") if fee_atoms else None

    # Output 2k: the treasury credit the covenant checks.
    tx.vout.append(TxOut(terms.payment_asset, plan.payment_atoms,
                         v1_script_pubkey(terms.treasury_prog)))

    # Output 2k+1: the remainder, or -- on a full buy -- something the covenant
    # will not mistake for one. If the token asset sat here on a full buy, the
    # leaf would read the buyer's own tokens as an unsold remainder and require
    # them to be sitting at the covenant address.
    if plan.remainder_atoms:
        tx.vout.append(TxOut(terms.token_asset, plan.remainder_atoms,
                             sale.cov.script_pubkey))
    elif change:
        asset = sorted(change)[0]
        tx.vout.append(TxOut(asset, change.pop(asset), change_spk))
    elif fee_out is not None:
        tx.vout.append(fee_out)
        fee_out = None
    else:
        raise BuildError(
            "a full buy needs a second output that is not the sale token, so "
            "the covenant does not read your tokens as an unsold remainder. "
            "Supply a change_script_pubkey or a fee.")

    # The buyer's tokens, at index 2 or later, where the covenant does not look.
    tx.vout.append(TxOut(terms.token_asset, plan.token_atoms, token_spk))

    for asset in sorted(change):
        tx.vout.append(TxOut(asset, change[asset], change_spk))

    if fee_out is not None:
        tx.vout.append(fee_out)

    _assert_balanced(tx, sale, plan, supplied)

    return {
        "unsigned_tx_hex": tx.hex(),
        "txid": tx.txid(),
        "vsize_estimate": _vsize(tx, len(ins)),
        "inputs": [
            {"index": 0, "role": "the sale covenant",
             "outpoint": "%s:%d" % (funding["txid"], funding["vout"]),
             "signing": "none: the sell leaf is introspection-driven, and its "
                        "witness is already in this transaction"},
        ] + [
            {"index": n + 1, "role": "yours",
             "outpoint": "%s:%d" % (i["txid"], i["vout"]),
             "signing": "sign with the key that controls this output"}
            for n, i in enumerate(ins)
        ],
        "outputs": [
            {"index": n, "asset": o.asset, "atoms": o.atoms,
             "script_pubkey": o.script_pubkey.hex(),
             "role": _role(n, o, sale, plan)}
            for n, o in enumerate(tx.vout)
        ],
    }


def _role(n, o, sale, plan):
    if n == 0:
        return "treasury credit, checked by the covenant"
    if n == 1 and plan.remainder_atoms:
        return "unsold remainder, re-rested at the sale address"
    if o.is_fee:
        return "network fee"
    if o.asset == sale.terms.token_asset:
        return "your tokens"
    return "your change"


def _assert_balanced(tx, sale, plan, supplied):
    """Every asset in must equal every asset out.

    Elements rejects an unbalanced transaction, but it rejects it after the
    buyer has signed and broadcast it. Checking here turns a confusing relay
    error into a clear refusal, and catches an accounting slip in this builder
    before it reaches a user.
    """
    ins = dict(supplied)
    ins[sale.terms.token_asset] = ins.get(sale.terms.token_asset, 0) + sale.locked_atoms
    outs = {}
    for o in tx.vout:
        outs[o.asset] = outs.get(o.asset, 0) + o.atoms
    if ins != outs:
        assets = sorted(set(ins) | set(outs))
        detail = "; ".join("%s in %d out %d" % (a, ins.get(a, 0), outs.get(a, 0))
                           for a in assets if ins.get(a, 0) != outs.get(a, 0))
        raise BuildError("transaction does not balance: %s" % detail)


def _vsize(tx, n_buyer_inputs):
    """A weight estimate that accounts for the signatures still to be added.

    The covenant input's witness is already present; each buyer input still
    needs a ~64-byte Schnorr signature (or a signature and a public key for a
    v0 input), which the caller has to pay for.
    """
    base = len(tx.serialize(with_witness=False))
    total = len(tx.serialize())
    total += n_buyer_inputs * 66
    weight = base * 3 + total
    return (weight + 3) // 4


def _need_spk(v, name):
    if not v:
        raise BuildError("%s is required" % name)
    if isinstance(v, str):
        try:
            b = bytes.fromhex(v)
        except ValueError:
            raise BuildError("%s must be a scriptPubKey in hex" % name)
    else:
        b = bytes(v)
    if not b:
        raise BuildError("%s cannot be empty (that is a fee output)" % name)
    return b


# --- the reclaim transaction -------------------------------------------------

SIGHASH_DEFAULT = 0


def taproot_sighash(tx, spent, input_index, genesis_hash, leaf_script=None,
                    hash_type=SIGHASH_DEFAULT):
    """The message a taproot spend signs, in Elements' variant of BIP341.

    Elements differs from Bitcoin in three ways that matter here: the genesis
    block hash is committed twice at the front (so a signature cannot be
    replayed onto another chain), each spent output contributes its asset AND
    value commitment rather than an 8-byte amount, and the witness data of every
    input and output is committed as well.

    `spent` is the list of outputs being spent, in input order, as
    (asset, atoms, script_pubkey) -- the signer has to know them, which is why a
    reclaim asks the project for its own input details.
    """
    if len(spent) != len(tx.vin):
        raise BuildError("need one spent output per input to sign")
    gen = bytes.fromhex(genesis_hash)[::-1] if isinstance(genesis_hash, str) else genesis_hash
    if len(gen) != 32:
        raise BuildError("genesis hash must be 32 bytes")

    def sha(b):
        return hashlib.sha256(b).digest()

    ss = gen + gen
    ss += bytes([hash_type])
    ss += struct.pack("<i", tx.version)
    ss += struct.pack("<I", tx.locktime)
    # No issuances and no pegins anywhere in a Levo transaction.
    ss += sha(b"\x00" * len(tx.vin))
    ss += sha(b"".join(bytes.fromhex(i.txid)[::-1] + struct.pack("<I", i.vout)
                       for i in tx.vin))
    ss += sha(b"".join(explicit_asset(a) + explicit_value(v) for a, v, _ in spent))
    ss += sha(b"".join(ser_string(_as_bytes(spk)) for _, _, spk in spent))
    ss += sha(b"".join(struct.pack("<I", i.sequence) for i in tx.vin))
    ss += sha(b"\x00" * len(tx.vin))                      # issuance blobs
    ss += sha(b"\x00\x00" * len(tx.vin))                  # issuance rangeproofs
    ss += sha(b"".join(o.serialize() for o in tx.vout))
    ss += sha(b"\x00\x00" * len(tx.vout))                 # output witnesses
    spend_type = 2 if leaf_script is not None else 0
    ss += bytes([spend_type])
    ss += struct.pack("<I", input_index)
    if leaf_script is not None:
        ss += K.tapleaf_hash(leaf_script)
        ss += b"\x00"
        ss += struct.pack("<i", -1)                        # no codeseparator
    expected = 366 + (37 if leaf_script is not None else 0)
    if len(ss) != expected:
        raise BuildError("sighash message is %d bytes, expected %d"
                         % (len(ss), expected))
    return S.tagged_hash("TapSighash/elements", ss)


def _as_bytes(v):
    return bytes.fromhex(v) if isinstance(v, str) else bytes(v)


def build_reclaim(sale, destination_spk, fee_inputs, fee_atoms, fee_asset,
                  genesis_hash, locktime=None, sign_with=None):
    """Sweep whatever did not sell, after the sale's close.

    The reclaim leaf is the one place in Levo where a covenant spend carries a
    signature, so this is the only builder that can be asked to sign. Pass
    `sign_with` (the reclaim private key as an int) to get a finished
    transaction; leave it out to get the sighash and sign elsewhere, which is
    what a project using a hardware signer will do.

    The covenant holds the sale token and nothing else, so the fee has to come
    from somewhere: `fee_inputs` are the project's own outputs covering it.
    """
    if not sale.funding:
        raise BuildError("this sale holds nothing to reclaim")
    lt = int(locktime if locktime is not None else sale.terms.close_locktime)
    if lt < sale.terms.close_locktime:
        raise BuildError(
            "locktime %d is before the sale's close of %d; the covenant would "
            "reject it" % (lt, sale.terms.close_locktime))

    tx = Transaction(version=2, locktime=lt)
    # CHECKLOCKTIMEVERIFY only takes effect when the input's sequence is not
    # final, so a reclaim with a final sequence fails on an otherwise correct
    # transaction.
    tx.vin.append(TxIn(sale.funding["txid"], sale.funding["vout"],
                       sequence=0xfffffffe))
    for i in fee_inputs or []:
        if i.get("blinded"):
            raise BuildError("confidential inputs cannot fund a reclaim; spend "
                             "an explicit output instead")
        tx.vin.append(TxIn(i["txid"], i["vout"], sequence=0xfffffffe))

    supplied = {}
    for i in fee_inputs or []:
        a = str(i["asset"]).lower()
        supplied[a] = supplied.get(a, 0) + int(i["value_atoms"])
    if supplied.get(fee_asset.lower(), 0) < int(fee_atoms):
        raise BuildError("fee inputs bring %d atoms of %s but the fee is %d"
                         % (supplied.get(fee_asset.lower(), 0), fee_asset, fee_atoms))

    tx.vout.append(TxOut(sale.terms.token_asset, sale.locked_atoms,
                         _as_bytes(destination_spk)))
    for asset, brought in sorted(supplied.items()):
        left = brought - (int(fee_atoms) if asset == fee_asset.lower() else 0)
        if left:
            tx.vout.append(TxOut(asset, left, _as_bytes(destination_spk)))
    if fee_atoms:
        tx.vout.append(TxOut(fee_asset, int(fee_atoms), b""))

    spent = [(sale.terms.token_asset, sale.locked_atoms, sale.cov.script_pubkey)]
    for i in fee_inputs or []:
        spent.append((str(i["asset"]).lower(), int(i["value_atoms"]),
                      _as_bytes(i["script_pubkey"])))

    sighash = taproot_sighash(tx, spent, 0, genesis_hash,
                              leaf_script=sale.cov.reclaim_leaf)

    result = {
        "sighash": sighash.hex(),
        "leaf": sale.cov.reclaim_leaf.hex(),
        "control_block": sale.cov.tap.control_block("reclaim").hex(),
        "locktime": lt,
        "signs_with": sale.terms.reclaim_xonly,
        "note": "sign the sighash with the reclaim key (BIP340) and put "
                "[signature, leaf, control block] in input 0's witness",
    }
    if sign_with is not None:
        sig = S.schnorr_sign(sighash, int(sign_with))
        if not S.schnorr_verify(sighash, sig, bytes.fromhex(sale.terms.reclaim_xonly)):
            raise BuildError(
                "that key does not match the sale's reclaim key; the covenant "
                "would reject this spend")
        tx.vin[0].witness = sale.cov.reclaim_witness(sig)
        result["signature"] = sig.hex()
    result["unsigned_tx_hex"] = tx.hex()
    result["txid"] = tx.txid()
    result["fee_inputs_still_to_sign"] = [
        {"index": n + 1, "outpoint": "%s:%d" % (i["txid"], i["vout"])}
        for n, i in enumerate(fee_inputs or [])]
    return result


# --- filling in a witness after the fact -------------------------------------

def set_witness(tx_hex, index, stack):
    """Replace one input's witness in an already-serialised transaction.

    A wallet signs the inputs it owns and leaves the covenant's alone, because
    it knows nothing about the leaf. The covenant's witness therefore has to go
    in afterwards, which means walking the serialisation to find the right
    span rather than rebuilding the transaction from scratch and risking a
    different one.
    """
    raw = bytes.fromhex(tx_hex) if isinstance(tx_hex, str) else bytes(tx_hex)
    pos = 4                                        # version
    flags = raw[pos]
    pos += 1
    if not flags & 1:
        raise BuildError("that transaction has no witness section to fill")

    def rd(p):
        n = raw[p]
        if n < 0xfd:
            return n, p + 1
        if n == 0xfd:
            return struct.unpack_from("<H", raw, p + 1)[0], p + 3
        if n == 0xfe:
            return struct.unpack_from("<I", raw, p + 1)[0], p + 5
        return struct.unpack_from("<Q", raw, p + 1)[0], p + 9

    n_in, pos = rd(pos)
    for _ in range(n_in):
        pos += 36                                  # outpoint
        ln, pos = rd(pos)
        pos += ln                                  # scriptSig
        pos += 4                                   # sequence
    n_out, pos = rd(pos)
    for _ in range(n_out):
        pos += 33 if raw[pos] == 1 else 1          # asset
        pos += 9 if raw[pos] == 1 else 33          # value
        pos += 1 if raw[pos] == 0 else 33          # nonce
        ln, pos = rd(pos)
        pos += ln                                  # scriptPubKey
    pos += 4                                       # locktime

    spans = []
    for _ in range(n_in):
        s0 = pos
        for _ in range(2):                         # issuance rangeproofs
            ln, pos = rd(pos)
            pos += ln
        for _ in range(2):                         # script witness, pegin witness
            cnt, pos = rd(pos)
            for _ in range(cnt):
                ln, pos = rd(pos)
                pos += ln
        spans.append((s0, pos))
    if not 0 <= index < len(spans):
        raise BuildError("input %d is not in this transaction" % index)
    a, b = spans[index]
    rebuilt = (ser_string(b"") + ser_string(b"")
               + ser_string_vector(list(stack)) + ser_string_vector([]))
    return (raw[:a] + rebuilt + raw[b:]).hex()
