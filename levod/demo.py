#!/usr/bin/env python3
"""Run Levo end to end without a Sequentia node.

Everything real stays real: the covenant addresses are derived by the same
builder, signatures are recovered the same way, and the tier arithmetic is the
shipped code. Only the node is replaced, by a stub that answers the read-only
questions levod asks -- who is staking, how tall is the chain, what rests at an
address, and what the fee table says.

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
        self.delegated = {}      # controller -> pool signer
        self.utxos = {}
        self.unspents = []
        self.height = 94_200

    def staker_weights(self):
        return dict(self.weights)

    def controller_weights(self):
        # An upgraded node's answer: weight keyed by the controller that owns
        # it, with the pool it is delegated to where that applies.
        return ({k: {"weight_atoms": v,
                     "delegated": k in self.delegated,
                     "signer": self.delegated.get(k)}
                 for k, v in self.weights.items()}, True)

    def delegations(self):
        return dict(self.delegated)

    def chain_height(self):
        return self.height

    def chain_name(self):
        return "demo"

    def median_time(self):
        return None

    def min_relay_fee_atoms_per_kvb(self):
        return 100_000

    def txout(self, txid, vout, include_mempool=True):
        return self.utxos.get((txid, int(vout)))

    def call(self, method, *params):
        if method == "getfeeexchangerates":
            return {"SBTC": 6400000000000, "USDX": 100000000}
        if method == "dumpassetlabels":
            return {"USDX": USDX}
        if method == "getblockhash":
            return "ddd11d54c87a2bd94400fd31ce05d8e1110bb4b78e7103f738342086fc4ea92e"
        if method == "scantxoutset":
            return {"success": True, "unspents": list(self.unspents)}
        if method == "getblockchaininfo":
            return {"blocks": self.height, "chain": "demo"}
        if method == "getrawmempool":
            return []
        raise RuntimeError("unexpected call %s" % method)


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
    # This stake is delegated to a pool. It still counts for the key that owns
    # it, which is the whole point: the signer view would show nothing here.
    node.delegated[stake_key] = "03" + "ab" * 32
    app.links.link(issuer, stake_key)

    listings = [
        dict(slug="helios-grid", name="Helios Grid", ticker="HLX",
             summary="Rooftop solar microgrids, financed by the households on them.",
             description=(
                 "Helios Grid builds neighbourhood-scale solar microgrids and sells "
                 "the output to the households connected to them.\n"
                 "Token holders take a share of metered revenue. This listing funds "
                 "the first four installations."),
             links={"Website": "https://example.com/helios",
                    "Registry entry": "https://example.com/registry/hlx"},
             token="a1" * 32, price_num=25_000_000, total=1_000_000, min_lot=50,
             fund=True),
        dict(slug="tidewater", name="Tidewater Freight", ticker="TDW",
             summary="Short-sea container routes along the Baltic coast.",
             description=(
                 "Tidewater runs small container vessels on routes the large "
                 "operators have abandoned.\n"
                 "This sale funds two additional hulls."),
             links={},
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
        meta = {k: L[k] for k in ("slug", "name", "ticker", "summary", "description", "links")}
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
            sale = app.market.projects[L["slug"]].sale
            sale.record_purchase("02" + "9a" * 32, 30_000 * 100_000_000, sold,
                                 txid=("%02x" % (0xe0 + i)) * 32, verified=True)
            sale.locked_atoms -= sold
            sale.sold_atoms += sold
            sale.status = S.PARTIAL
            sale.funding = {"txid": txid, "vout": 1, "atoms": sale.locked_atoms,
                            "height": node.height - 3, "block": "demo-block"}
            node.utxos[(txid, 1)] = {
                "scriptPubKey": {"hex": p.sale.script_pubkey},
                "asset": L["token"], "valueatoms": sale.locked_atoms,
                "confirmations": 3}
            del node.utxos[(txid, 0)]
            app.market.save()

    # A buyer's wallet, for clicking through a purchase: unblinded USDX at a
    # v0 address, which is what a browser wallet's own outputs look like.
    node.utxos[("dd" * 32, 0)] = {
        "scriptPubKey": {"hex": "0014" + "cc" * 20},
        "asset": USDX, "valueatoms": 5_000 * 100_000_000, "confirmations": 12}
    return issuer


def main():
    state = Path(tempfile.gettempdir()) / "levo-demo-state.json"
    if state.exists():
        state.unlink()
    os.environ["LEVOD_STATE"] = str(state)
    os.environ.setdefault("LEVOD_SECRET", "levo-demo-secret")

    import server

    node = StubNode()
    app = server.App(node=node)
    server.Handler.app = app

    issuer = build(app, node)

    host = os.environ.get("LEVOD_HOST", "127.0.0.1")
    port = int(os.environ.get("LEVOD_PORT", "8099"))
    srv = ThreadingHTTPServer((host, port), server.Handler)
    print("Levo demo on http://%s:%d" % (host, port), flush=True)
    print("  serving %s" % app.webroot, flush=True)
    print("  seeded issuer account %s" % issuer, flush=True)
    if not (app.webroot / "index.html").is_file():
        print("  NOTE: the web app is not built yet -- run: cd web && npm install && npm run build",
              flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
