"""Elements PSET v2: the form a browser wallet signs.

A raw unsigned transaction is enough for a node, whose `signrawtransactionwithwallet`
looks every input up for itself. A browser wallet gets no such look-up: it is
handed a document that carries, for every input, the output being spent, and
it signs exactly the inputs it recognises as its own. That document is a PSET,
the Elements form of a partially signed Bitcoin transaction.

The covenant input is the reason this file exists. The sell leaf needs no
signature, so its witness -- the leaf script and its control block -- is known
before anybody signs, and it goes into the PSET as the input's FINAL witness. A
wallet signs the buyer's inputs, leaves the covenant's alone, finalises, and
broadcasts. Nothing the wallet does can alter the covenant's half.

The encoding below follows what a Sequentia node emits and accepts: the
key/value layout, the proprietary `pset` keys, the internal byte order of
asset ids and txids, and the big-endian explicit value. `tests/test_pset.py`
round-trips what this produces through the parser here, and the node
integration test hands it to a real node to decode, sign, finalise and relay.
"""

import base64
import struct

import covenant as C

MAGIC = b"pset\xff"

# Key types, as the node writes them.
GLOBAL_TX_VERSION = 0x02
GLOBAL_FALLBACK_LOCKTIME = 0x03
GLOBAL_INPUT_COUNT = 0x04
GLOBAL_OUTPUT_COUNT = 0x05
GLOBAL_VERSION = 0xfb

IN_WITNESS_UTXO = 0x01
IN_FINAL_SCRIPTWITNESS = 0x08
IN_PREVIOUS_TXID = 0x0e
IN_OUTPUT_INDEX = 0x0f
IN_SEQUENCE = 0x10

OUT_AMOUNT = 0x03
OUT_SCRIPT = 0x04

# Elements proprietary keys: 0xfc <varstr "pset"> <subtype>
PROPRIETARY = 0xfc
PSET_PREFIX = b"pset"
PSET_OUT_ASSET = 0x02
PSET_OUT_BLINDER_INDEX = 0x08

SEPARATOR = b"\x00"


def compact_size(n):
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def varbytes(b):
    return compact_size(len(b)) + bytes(b)


def _u32(n):
    return struct.pack("<I", int(n))


def _bytes(v):
    if v is None:
        return b""
    return bytes.fromhex(v) if isinstance(v, str) else bytes(v)


def explicit_txout(asset_display_hex, atoms, script_pubkey):
    """An unblinded output as PSBT_IN_WITNESS_UTXO carries it.

    asset   0x01 || 32 bytes in WIRE order (the display id reversed)
    value   0x01 || 8 bytes BIG-endian, unlike every other integer here
    nonce   0x00, because the output is explicit
    script  varbytes
    """
    atoms = int(atoms)
    if not 0 <= atoms < (1 << 63):
        raise ValueError("value out of range: %d" % atoms)
    return (b"\x01" + C.asset_to_wire(asset_display_hex)
            + b"\x01" + atoms.to_bytes(8, "big")
            + b"\x00"
            + varbytes(_bytes(script_pubkey)))


def _kv(key_type, value, key_data=b""):
    return varbytes(bytes([key_type]) + key_data) + varbytes(value)


def _prop(subtype, value):
    key = bytes([PROPRIETARY]) + varbytes(PSET_PREFIX) + bytes([subtype])
    return varbytes(key) + varbytes(value)


def build_pset(inputs, outputs, version=2, locktime=0):
    """Encode a PSET and return it base64, the form every wallet API takes.

    inputs:  [{txid, vout, sequence?, witness_utxo: {asset, atoms, script_pubkey},
               final_witness?: [bytes|hex, ...]}]
    outputs: [{asset, atoms, script_pubkey}]   -- the fee output has an empty script

    `asset` and `txid` are display hex; the reversing onto the wire happens here
    so no caller has to remember. Every output is explicit: a covenant reads the
    values it checks and can police nothing it cannot read. What makes an output
    explicit is the absence of a blinding pubkey, which is what a wallet reads to
    decide whether an output is blinded at all; the blinder index written beside
    it names which input's owner would do the blinding, and with no pubkey to go
    with it, it does nothing.
    """
    if not inputs:
        raise ValueError("a PSET needs at least one input")
    if not outputs:
        raise ValueError("a PSET needs at least one output")
    out = bytearray(MAGIC)
    out += _kv(GLOBAL_TX_VERSION, _u32(version))
    out += _kv(GLOBAL_FALLBACK_LOCKTIME, _u32(locktime))
    out += _kv(GLOBAL_INPUT_COUNT, compact_size(len(inputs)))
    out += _kv(GLOBAL_OUTPUT_COUNT, compact_size(len(outputs)))
    out += _kv(GLOBAL_VERSION, _u32(2))
    out += SEPARATOR
    for i in inputs:
        wu = i.get("witness_utxo")
        if wu:
            out += _kv(IN_WITNESS_UTXO, explicit_txout(
                wu["asset"], wu["atoms"], wu["script_pubkey"]))
        fw = i.get("final_witness")
        if fw:
            stack = b"".join(varbytes(_bytes(w)) for w in fw)
            out += _kv(IN_FINAL_SCRIPTWITNESS, compact_size(len(fw)) + stack)
        out += _kv(IN_PREVIOUS_TXID, bytes.fromhex(i["txid"])[::-1])
        out += _kv(IN_OUTPUT_INDEX, _u32(i["vout"]))
        seq = i.get("sequence")
        out += _kv(IN_SEQUENCE, _u32(0xffffffff if seq is None else seq))
        out += SEPARATOR
    for o in outputs:
        atoms = int(o["atoms"])
        if not 0 <= atoms < (1 << 63):
            raise ValueError("value out of range: %d" % atoms)
        out += _kv(OUT_AMOUNT, struct.pack("<Q", atoms))
        out += _kv(OUT_SCRIPT, _bytes(o.get("script_pubkey")))
        out += _prop(PSET_OUT_ASSET, C.asset_to_wire(o["asset"]))
        out += _prop(PSET_OUT_BLINDER_INDEX, _u32(0))
        out += SEPARATOR
    return base64.b64encode(bytes(out)).decode()


def from_transaction(tx, spent):
    """The PSET for an assembled `tx.Transaction`, given the outputs it spends.

    `spent` is one {asset, atoms, script_pubkey} per input, in input order. An
    input that already carries a witness (the covenant) has it written as its
    final witness, so the wallet leaves it alone.
    """
    if len(spent) != len(tx.vin):
        raise ValueError("need one spent output per input")
    ins = []
    for vin, sp in zip(tx.vin, spent):
        ins.append({"txid": vin.txid, "vout": vin.vout, "sequence": vin.sequence,
                    "witness_utxo": {"asset": sp["asset"], "atoms": sp["atoms"],
                                     "script_pubkey": sp["script_pubkey"]},
                    "final_witness": list(vin.witness) or None})
    outs = [{"asset": o.asset, "atoms": o.atoms, "script_pubkey": o.script_pubkey}
            for o in tx.vout]
    return build_pset(ins, outs, version=tx.version, locktime=tx.locktime)


# --- reading one back --------------------------------------------------------

def _read_compact(b, i):
    n = b[i]
    if n < 0xfd:
        return n, i + 1
    if n == 0xfd:
        return struct.unpack_from("<H", b, i + 1)[0], i + 3
    if n == 0xfe:
        return struct.unpack_from("<I", b, i + 1)[0], i + 5
    return struct.unpack_from("<Q", b, i + 1)[0], i + 9


def parse_maps(pset_b64):
    """The raw key/value maps of a PSET: [global, input..., output...].

    Enough to check what was written, which is what the tests need; it is not
    a general PSBT parser and does not interpret the fields.
    """
    blob = base64.b64decode(pset_b64)
    if blob[:5] != MAGIC:
        raise ValueError("not a PSET")
    i = 5
    maps, cur = [], []
    while i < len(blob):
        klen, i = _read_compact(blob, i)
        if klen == 0:
            maps.append(cur)
            cur = []
            continue
        key = blob[i:i + klen]
        i += klen
        vlen, i = _read_compact(blob, i)
        cur.append((bytes(key), bytes(blob[i:i + vlen])))
        i += vlen
    if cur:
        raise ValueError("PSET ends inside a map")
    return maps
