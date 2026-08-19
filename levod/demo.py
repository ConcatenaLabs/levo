#!/usr/bin/env python3
"""Run Levo end to end without a Sequentia node.

Everything real stays real: the covenant addresses are derived by the same
builder, signatures are recovered the same way, and the tier arithmetic is the
shipped code. Only the node is replaced, by a stub that answers the three
questions levod asks -- who is staking, how tall is the chain, and is this
output still unspent.

    python3 levod/demo.py            # then open http://127.0.0.1:8099

Use it to click through the platform, not to prove anything about the chain. A
sale here is funded because the stub says so.
"""

import os
import sys
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))

FLOOR = 4_000_000_000_000          # 40,000 SEQ
USDX = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"


class StubNode:
    def __init__(self):
        self.weights = {}
        self.utxos = {}
        self.height = 94_200

    def staker_weights(self):
        return dict(self.weights)

    def chain_height(self):
        return self.height

    def txout(self, txid, vout, include_mempool=True):
        return self.utxos.get((txid, int(vout)))


def build(app, node):
    """Seed two sales: one open, one still a draft."""
    import covenant as C
    import market as M
    import sale as S
    import signhelper as SH

    issuer_sec = 0x51ee0000000000000000000000000000000000000000000000000000000000a1
    issuer = SH.pubkey_of(issuer_sec)
    stake_key = SH.pubkey_of(0x51ee0000000000000000000000000000000000000000000000000000000000a2)
    node.weights[stake_key] = 30 * FLOOR
    app.links.link(issuer, stake_key)

    listings = [
        dict(slug="helios-grid", name="Helios Grid", ticker="HLX",
             summary="Rooftop solar microgrids, financed by the households on them.",
             description=(
                 "Helios Grid builds neighbourhood-scale solar microgrids and sells "
                 "the output to the households connected to them.\n"
                 "Token holders take a share of metered revenue. This listing funds "
                 "the first four installations."),
             token="a1" * 32, price_num=25_000_000, total=1_000_000, min_lot=50,
             fund=True),
        dict(slug="tidewater", name="Tidewater Freight", ticker="TDW",
             summary="Short-sea container routes along the Baltic coast.",
             description=(
                 "Tidewater runs small container vessels on routes the large "
                 "operators have abandoned.\n"
                 "This sale funds two additional hulls."),
             token="b2" * 32, price_num=8_000_000, total=500_000, min_lot=100,
             fund=False),
    ]

    for i, L in enumerate(listings):
        terms = {
            "token_asset": L["token"], "payment_asset": USDX,
            "price_num": L["price_num"], "price_den": 100_000_000,
            "treasury_prog": ("%02x" % (0x30 + i)) * 32,
            "min_lot": L["min_lot"] * 100_000_000,
            "close_locktime": 2_000_000_000,
            "reclaim_xonly": ("%02x" % (0x40 + i)) * 32,
            "total_atoms": L["total"] * 100_000_000,
        }
        meta = {k: L[k] for k in ("slug", "name", "ticker", "summary", "description")}
        meta["links"] = {}
        p = app.market.list_project(issuer, meta, terms)
        if L["fund"]:
            txid = ("%02x" % (0xf0 + i)) * 32
            node.utxos[(txid, 0)] = {
                "scriptPubKey": {"hex": p.sale.script_pubkey},
                "asset": L["token"],
                "valueatoms": terms["total_atoms"],
            }
            app.market.confirm_lock(issuer, L["slug"], txid, 0)
            # A previous buyer has already taken a slice, so the sale shows a
            # partial fill resting at the same address.
            sold = 120_000 * 100_000_000
            app.market.projects[L["slug"]].sale.record_purchase(
                "02" + "9a" * 32, 30_000 * 100_000_000, sold)
            app.market.projects[L["slug"]].sale.funding = {
                "txid": txid, "vout": 1,
                "atoms": app.market.projects[L["slug"]].sale.locked_atoms}
            app.market.save()

    return issuer


def main():
    state = Path(tempfile.gettempdir()) / "levo-demo-state.json"
    if state.exists():
        state.unlink()
    os.environ["LEVOD_STATE"] = str(state)
    os.environ.setdefault("LEVOD_SECRET", "levo-demo-secret")

    import server

    app = server.App()
    node = StubNode()
    app.node = node
    app.reader.rpc = node
    app.market.rpc = node
    server.Handler.app = app

    issuer = build(app, node)

    host = os.environ.get("LEVOD_HOST", "127.0.0.1")
    port = int(os.environ.get("LEVOD_PORT", "8099"))
    srv = ThreadingHTTPServer((host, port), server.Handler)
    print("Levo demo on http://%s:%d" % (host, port))
    print("  serving %s" % app.webroot)
    print("  seeded issuer account %s" % issuer)
    if not (app.webroot / "index.html").is_file():
        print("  NOTE: the web app is not built yet -- run: cd web && npm install && npm run build")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
