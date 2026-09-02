"""PSET checks: the encoder against its own parser and against the layout a
Sequentia node emits. The node integration test hands the real thing to a real
node; this pins the bytes so a change here is noticed before that runs."""

import base64
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import covenant as C  # noqa: E402
import pset as P  # noqa: E402
import tx as TX  # noqa: E402

USDX = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"
GOLD = "3a0f9192219db59f8d7f87d93ac6311095dfe1255d149727b87baaa7d2cc71a1"


def _maps(b64):
    return P.parse_maps(b64)


def _get(m, key_hex):
    for k, v in m:
        if k.hex() == key_hex:
            return v
    return None


def test_layout_matches_the_node(t):
    b64 = P.build_pset(
        [{"txid": "ab" * 32, "vout": 1, "sequence": 0xffffffff,
          "witness_utxo": {"asset": USDX, "atoms": 5, "script_pubkey": "0014" + "cc" * 20},
          "final_witness": ["01", "7551"]},
         {"txid": "cd" * 32, "vout": 0,
          "witness_utxo": {"asset": GOLD, "atoms": 7, "script_pubkey": "5120" + "dd" * 32}}],
        [{"asset": GOLD, "atoms": 7, "script_pubkey": "5120" + "ee" * 32},
         {"asset": USDX, "atoms": 5, "script_pubkey": ""}])
    raw = base64.b64decode(b64)
    t.eq(raw[:5], b"pset\xff", "magic")
    g, i0, i1, o0, o1 = _maps(b64)
    t.eq(_get(g, "02"), struct.pack("<I", 2), "tx version")
    t.eq(_get(g, "03"), struct.pack("<I", 0), "fallback locktime")
    t.eq(_get(g, "04"), b"\x02", "input count")
    t.eq(_get(g, "05"), b"\x02", "output count")
    t.eq(_get(g, "fb"), struct.pack("<I", 2), "PSBT version 2")
    t.eq(_get(i0, "0e"), bytes.fromhex("ab" * 32)[::-1], "previous txid is in wire order")
    t.eq(_get(i0, "0f"), struct.pack("<I", 1), "output index")
    t.eq(_get(i0, "10"), struct.pack("<I", 0xffffffff), "sequence as given")
    t.eq(_get(i1, "10"), struct.pack("<I", 0xffffffff), "default sequence is final")
    wu = _get(i0, "01")
    t.eq(wu[:33], b"\x01" + C.asset_to_wire(USDX), "witness utxo asset is explicit and wire-ordered")
    t.eq(wu[33:42], b"\x01" + (5).to_bytes(8, "big"), "witness utxo value is explicit and big-endian")
    t.eq(wu[42:43], b"\x00", "nonce is empty")
    t.eq(wu[43:], bytes([22]) + bytes.fromhex("0014" + "cc" * 20), "script is varbytes")
    t.eq(_get(i0, "08"), b"\x02" + b"\x01\x01" + b"\x02\x75\x51", "final witness stack")
    t.eq(_get(i1, "08"), None, "an input without a witness carries none")
    t.eq(_get(o0, "03"), struct.pack("<Q", 7), "output amount, little-endian")
    t.eq(_get(o0, "04"), bytes.fromhex("5120" + "ee" * 32), "output script raw")
    t.eq(_get(o1, "04"), b"", "the fee output has an empty script")
    prop_asset = bytes([0xfc, 4]) + b"pset" + bytes([0x02])
    t.eq(_get(o0, prop_asset.hex()), C.asset_to_wire(GOLD), "proprietary asset key, wire order")
    prop_blinder = bytes([0xfc, 4]) + b"pset" + bytes([0x08])
    t.eq(_get(o0, prop_blinder.hex()), struct.pack("<I", 0), "blinder index zero: explicit")


def test_from_transaction_carries_the_covenant_witness(t):
    tx = TX.Transaction()
    tx.vin.append(TX.TxIn("ab" * 32, 1, witness=[b"\x51", b"\xc4" + b"\x00" * 64]))
    tx.vin.append(TX.TxIn("cd" * 32, 0))
    tx.vout.append(TX.TxOut(USDX, 100, "5120" + "11" * 32))
    tx.vout.append(TX.TxOut(USDX, 1, b""))
    b64 = P.from_transaction(tx, [
        {"asset": GOLD, "atoms": 9, "script_pubkey": "5120" + "22" * 32},
        {"asset": USDX, "atoms": 101, "script_pubkey": "0014" + "33" * 20}])
    g, i0, i1, o0, o1 = _maps(b64)
    t.ok(_get(i0, "08") is not None, "the covenant input has its final witness")
    t.eq(_get(i1, "08"), None, "the buyer's input is left for the wallet")
    t.eq(_get(i0, "10"), struct.pack("<I", 0xffffffff), "sequence matches the raw transaction")
    try:
        P.from_transaction(tx, [{"asset": GOLD, "atoms": 9, "script_pubkey": "51"}])
        t.ok(False, "one spent output per input is required")
    except ValueError:
        t.ok(True, "one spent output per input is required")


def test_bad_inputs_are_refused(t):
    for args, what in (
        (([], [{"asset": USDX, "atoms": 1, "script_pubkey": ""}]), "no inputs"),
        (([{"txid": "ab" * 32, "vout": 0}], []), "no outputs"),
        (([{"txid": "ab" * 32, "vout": 0}], [{"asset": USDX, "atoms": -1, "script_pubkey": ""}]), "negative amount"),
    ):
        try:
            P.build_pset(*args)
            t.ok(False, what + " refused")
        except ValueError:
            t.ok(True, what + " refused")
