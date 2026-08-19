#!/usr/bin/env python3
"""Freeze the sale-covenant bytes Levo is verified against.

Run once, commit the result, and never regenerate it to make a failing check
pass. `levod/covenant.py` compares the live builder against these on every
import; a mismatch means sale addresses have MOVED, and tokens locked under the
old bytes are not at the address the new bytes derive.

If the upstream covenant genuinely changes, that is a migration -- existing
sales must be closed out under the old bytes first -- not a vector refresh.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "levod"))
import covenant as L  # noqa: E402

CASES = [
    # A plain USDX-priced sale.
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


def main():
    out = {"note": "Frozen sale-covenant bytes. See tools/gen_vectors.py.",
           "cases": []}
    for c in CASES:
        terms = L.SaleTerms(c["token"], c["payment"], c["rate_num"], c["rate_den"],
                            c["treasury_prog"], c["min_lot"], c["close_locktime"],
                            c["reclaim_x"])
        cov = L.derive(terms)
        out["cases"].append({
            "name": c["name"],
            "token": c["token"], "payment": c["payment"],
            "rate_num": c["rate_num"], "rate_den": c["rate_den"],
            "treasury_prog": c["treasury_prog"], "min_lot": c["min_lot"],
            "close_locktime": c["close_locktime"], "reclaim_x": c["reclaim_x"],
            "expect": {
                "sell_leaf": cov.sell_leaf.hex(),
                "reclaim_leaf": cov.reclaim_leaf.hex(),
                "spk": cov.spk_hex,
            },
        })
    dest = Path(__file__).resolve().parent.parent / "levod" / "vectors.json"
    dest.write_text(json.dumps(out, indent=2) + "\n")
    print("wrote %s (%d cases)" % (dest, len(out["cases"])))


if __name__ == "__main__":
    main()
