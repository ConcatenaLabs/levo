#!/usr/bin/env python3
"""Freeze the sale-covenant bytes Levo is verified against.

Run once, commit the result, and never regenerate it to make a failing check
pass. `levod/covenant.py` compares the live builder against these on every
import; a mismatch means sale addresses have MOVED, and tokens locked under the
old bytes are not at the address the new bytes derive.

If the upstream covenant genuinely changes, that is a migration -- existing
sales must be closed out under the old bytes first -- not a vector refresh.

The cases feed the RAW leaf builders with asset ids in WIRE order, exactly as
`covenant._check_vectors` reads them back. The display-to-wire reversal that
`SaleTerms` performs is a separate layer with its own tests, and keeping it out
of the vectors is what lets the vectors pin the bytes and nothing else.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "levod"))
import covenant as L  # noqa: E402
import script as K  # noqa: E402

CASES = [
    # A plain USDX-priced sale. `payment` is USDX's DISPLAY id: the vector
    # feeds it to the raw builder as given, so the leaf pins these bytes.
    dict(name="usdx-simple",
         token="aa" * 32,
         payment="2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de",
         rate_num=25, rate_den=100, treasury_prog="11" * 32,
         min_lot=100000, close_locktime=1000000, reclaim_x="22" * 32),
    # A sale whose price does not divide evenly, exercising the ceiling.
    dict(name="usdx-ceil",
         token="bb" * 32,
         payment="2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de",
         rate_num=7, rate_den=3, treasury_prog="33" * 32,
         min_lot=1, close_locktime=987654, reclaim_x="44" * 32),
    # Minimum-everything, to pin the degenerate end of the parameter space.
    dict(name="minimal",
         token="01" * 32, payment="02" * 32,
         rate_num=1, rate_den=1, treasury_prog="00" * 32,
         min_lot=1, close_locktime=1, reclaim_x="ff" * 32),
]


def build(cases=CASES):
    out = {"note": "Frozen sale-covenant bytes. See tools/gen_vectors.py.",
           "cases": []}
    for c in cases:
        sell = L.build_sell_leaf(bytes.fromhex(c["token"]), bytes.fromhex(c["payment"]),
                                 c["rate_num"], c["rate_den"],
                                 bytes.fromhex(c["treasury_prog"]), c["min_lot"])
        reclaim = L.build_reclaim_leaf(c["close_locktime"], bytes.fromhex(c["reclaim_x"]))
        tap = K.Taptree(K.NUMS, [("sell", sell), ("reclaim", reclaim)])
        out["cases"].append({
            "name": c["name"],
            "token": c["token"], "payment": c["payment"],
            "rate_num": c["rate_num"], "rate_den": c["rate_den"],
            "treasury_prog": c["treasury_prog"], "min_lot": c["min_lot"],
            "close_locktime": c["close_locktime"], "reclaim_x": c["reclaim_x"],
            "expect": {
                "sell_leaf": sell.hex(),
                "reclaim_leaf": reclaim.hex(),
                "spk": tap.script_pubkey.hex(),
            },
        })
    return out


def main():
    out = build()
    dest = Path(__file__).resolve().parent.parent / "levod" / "vectors.json"
    dest.write_text(json.dumps(out, indent=2) + "\n")
    print("wrote %s (%d cases)" % (dest, len(out["cases"])))


if __name__ == "__main__":
    main()
