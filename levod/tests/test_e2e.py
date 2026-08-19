"""End-to-end drill: stake, sign in, list, lock, buy -- against a stub node.

This runs the real HTTP server and the real covenant builder. Only the node is
stubbed, so what it proves is the whole platform except chain propagation: the
sale address is derived by the proven builder, the login signature is really
recovered, and the tier caps really refuse an over-large buy.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import signhelper as SH  # noqa: E402


class FakeNode:
    """Just enough node to answer Levo's three questions."""

    def __init__(self):
        self.weights = {}
        self.utxos = {}          # (txid, vout) -> output dict
        self.height = 12345

    def staker_weights(self):
        return dict(self.weights)

    def chain_height(self):
        return self.height

    def txout(self, txid, vout, include_mempool=True):
        return self.utxos.get((txid, int(vout)))


def _req(base, method, path, body=None, token=None):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    import os
    state = HERE / "_e2e-state.json"
    if state.exists():
        state.unlink()
    os.environ["LEVOD_STATE"] = str(state)
    os.environ["LEVOD_SECRET"] = "test-secret-not-used-in-production"

    import server  # noqa: E402

    app = server.App()
    node = FakeNode()
    app.node = node
    app.reader.rpc = node
    app.market.rpc = node
    server.Handler.app = app

    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    ok = Checker()

    # --- the cast ---------------------------------------------------------
    issuer_sec = 0x11abcdef11abcdef11abcdef11abcdef11abcdef11abcdef11abcdef11abcdef
    buyer_sec = 0x22abcdef22abcdef22abcdef22abcdef22abcdef22abcdef22abcdef22abcdef
    issuer_stake_sec = 0x33abcdef33abcdef33abcdef33abcdef33abcdef33abcdef33abcdef33abcdef
    buyer_stake_sec = 0x44abcdef44abcdef44abcdef44abcdef44abcdef44abcdef44abcdef44abcdef

    issuer_stake_pk = SH.pubkey_of(issuer_stake_sec)
    buyer_stake_pk = SH.pubkey_of(buyer_stake_sec)
    # Issuer holds the top tier; buyer holds the first tier.
    node.weights[issuer_stake_pk] = 25 * 4_000_000_000_000
    node.weights[buyer_stake_pk] = 4_000_000_000_000

    def sign_in(sec):
        _, ch = _req(base, "POST", "/api/auth/challenge")
        sig = SH.sign_recoverable(sec, ch["message"])
        code, r = _req(base, "POST", "/api/auth/verify",
                       {"message": ch["message"], "signature": sig})
        ok.eq(code, 200, "sign in")
        ok.eq(r["account"], SH.pubkey_of(sec), "account is the signing key")
        return r["token"]

    def link_stake(token, account_sec, stake_sec):
        pk = SH.pubkey_of(stake_sec)
        _, ch = _req(base, "POST", "/api/stake/challenge",
                     {"staker_pubkey": pk}, token=token)
        sig = SH.sign_recoverable(stake_sec, ch["message"])
        code, r = _req(base, "POST", "/api/stake/link",
                       {"message": ch["message"], "signature": sig,
                        "staker_pubkey": pk}, token=token)
        ok.eq(code, 200, "link staking key")
        return r

    # --- health -----------------------------------------------------------
    code, r = _req(base, "GET", "/api/health")
    ok.eq(code, 200, "health")
    ok.eq(r["node"]["reachable"], True, "node reachable")

    # --- sign in ----------------------------------------------------------
    issuer_tok = sign_in(issuer_sec)
    buyer_tok = sign_in(buyer_sec)

    # A stranger cannot claim someone else's stake by naming their key.
    pk = issuer_stake_pk
    _, ch = _req(base, "POST", "/api/stake/challenge",
                 {"staker_pubkey": pk}, token=buyer_tok)
    bad = SH.sign_recoverable(buyer_stake_sec, ch["message"])   # wrong key signs
    code, r = _req(base, "POST", "/api/stake/link",
                   {"message": ch["message"], "signature": bad,
                    "staker_pubkey": pk}, token=buyer_tok)
    ok.eq(code, 400, "cannot link a staking key you do not control")

    st = link_stake(issuer_tok, issuer_sec, issuer_stake_sec)
    ok.eq(st["tier"]["name"], "Founder", "issuer reaches the top tier")
    ok.eq(st["tier"]["may_list"], True, "top tier may list")

    st = link_stake(buyer_tok, buyer_sec, buyer_stake_sec)
    ok.eq(st["tier"]["name"], "Contributor", "buyer reaches tier 1")
    ok.eq(st["tier"]["may_list"], False, "tier 1 may not list")

    # --- listing is gated -------------------------------------------------
    token_asset = "aa" * 32
    usdx = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"
    total = 1_000_000 * 100_000_000          # 1,000,000 tokens at 8dp
    terms = {
        "token_asset": token_asset, "payment_asset": usdx,
        "price_num": 25, "price_den": 100,        # 0.25 USDX per token
        "treasury_prog": "11" * 32, "min_lot": 100_000,
        "close_locktime": 2_000_000_000, "reclaim_xonly": "22" * 32,
        "total_atoms": total,
    }
    meta = {"slug": "helios", "name": "Helios Grid", "ticker": "HLX",
            "summary": "Solar microgrids, tokenised.",
            "description": "A demonstration listing.", "links": {}}

    code, r = _req(base, "POST", "/api/projects",
                   {"project": meta, "terms": terms}, token=buyer_tok)
    ok.eq(code, 403, "tier 1 cannot list a project")

    code, r = _req(base, "POST", "/api/projects",
                   {"project": meta, "terms": terms}, token=issuer_tok)
    ok.eq(code, 201, "top tier can list")
    spk = r["lock"]["script_pubkey"]

    # The address must be reproducible from the PUBLISHED terms -- which is what
    # a client reads back, and which carry the price in lowest terms.
    import covenant as C
    published = r["project"]["sale"]["terms"]
    ok.eq(C.derive(C.SaleTerms.from_json(published)).spk_hex, spk,
          "sale address is reproducible from published terms")
    ok.eq((published["price_num"], published["price_den"]), (1, 4),
          "the submitted price 25/100 is stored in lowest terms")
    ok.eq(r["lock"]["price_reduced"], True, "the response says the price was reduced")
    # Reducing must not change what anything costs.
    ok.eq(C.SaleTerms.from_json(published).cost_for(1_000 * 100_000_000),
          250 * 100_000_000, "reducing the price changes no cost")

    # --- not investable until locked --------------------------------------
    code, r = _req(base, "POST", "/api/projects/helios/buy",
                   {"token_atoms": 100_000_000}, token=buyer_tok)
    ok.eq(code, 400, "cannot buy before the tokens are locked")

    # A lock claim that points at the wrong script is refused.
    node.utxos[("cc" * 32, 0)] = {"scriptPubKey": {"hex": "51200000"},
                                  "asset": token_asset, "valueatoms": total}
    code, r = _req(base, "POST", "/api/projects/helios/lock",
                   {"txid": "cc" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 400, "a lock at the wrong address is refused")

    # A lock holding the wrong asset is refused.
    node.utxos[("dd" * 32, 0)] = {"scriptPubKey": {"hex": spk},
                                  "asset": "bb" * 32, "valueatoms": total}
    code, r = _req(base, "POST", "/api/projects/helios/lock",
                   {"txid": "dd" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 400, "a lock holding the wrong asset is refused")

    # A short lock is refused.
    node.utxos[("ee" * 32, 0)] = {"scriptPubKey": {"hex": spk},
                                  "asset": token_asset, "valueatoms": total - 1}
    code, r = _req(base, "POST", "/api/projects/helios/lock",
                   {"txid": "ee" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 400, "a short lock is refused")

    # The real lock.
    node.utxos[("ff" * 32, 0)] = {"scriptPubKey": {"hex": spk},
                                  "asset": token_asset, "valueatoms": total}
    code, r = _req(base, "POST", "/api/projects/helios/lock",
                   {"txid": "ff" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 200, "the correct lock is accepted")
    ok.eq(r["sale"]["status"], "live", "sale goes live once locked")

    # Only the issuer may confirm a lock.
    code, r = _req(base, "POST", "/api/projects/helios/lock",
                   {"txid": "ff" * 32, "vout": 0}, token=buyer_tok)
    ok.eq(code, 403, "a stranger cannot confirm someone else's lock")

    # --- buying -----------------------------------------------------------
    code, plan = _req(base, "POST", "/api/projects/helios/buy",
                      {"token_atoms": 1_000 * 100_000_000}, token=buyer_tok)
    ok.eq(code, 200, "tier 1 buy within cap")
    ok.eq(plan["payment_atoms"], 250 * 100_000_000, "1,000 tokens cost 250 USDX")
    ok.eq(plan["required_outputs"][0]["index"], 0, "treasury credit at output 2k")
    ok.eq(plan["required_outputs"][1]["index"], 1, "remainder re-rests at 2k+1")
    ok.eq(plan["required_outputs"][1]["script_pubkey"], spk,
          "remainder returns to the identical covenant")
    ok.eq(plan["cap"]["enforced_by"], "levo", "caps are labelled as policy")
    ok.eq(len(plan["covenant"]["witness"]), 2, "sell witness is leaf + control block")

    # Over the tier cap.
    code, r = _req(base, "POST", "/api/projects/helios/buy",
                   {"token_atoms": 100_000 * 100_000_000}, token=buyer_tok)
    ok.eq(code, 409, "a buy beyond the tier cap is refused")

    # Below the minimum lot.
    code, r = _req(base, "POST", "/api/projects/helios/buy",
                   {"token_atoms": 10}, token=buyer_tok)
    ok.eq(code, 400, "a buy below the minimum lot is refused")

    # Anonymous buyers are refused.
    code, r = _req(base, "POST", "/api/projects/helios/buy",
                   {"token_atoms": 100_000_000})
    ok.eq(code, 401, "buying requires a signed-in wallet")

    # --- settling ---------------------------------------------------------
    # The covenant output is spent by the buyer's transaction.
    del node.utxos[("ff" * 32, 0)]
    code, r = _req(base, "POST", "/api/projects/helios/confirm",
                   {"txid": "ab" * 32, "token_atoms": 1_000 * 100_000_000,
                    "payment_atoms": 250 * 100_000_000}, token=buyer_tok)
    ok.eq(code, 200, "purchase recorded")
    ok.eq(r["status"], "partial", "a partial buy leaves the sale resting")
    ok.eq(r["remaining_atoms"], total - 1_000 * 100_000_000, "remainder tracked")

    # The cap is cumulative across purchases, not per purchase.
    code, r = _req(base, "POST", "/api/projects/helios/buy",
                   {"payment_atoms": 800 * 100_000_000}, token=buyer_tok)
    ok.eq(code, 409, "the tier cap is cumulative across purchases")

    code, r = _req(base, "POST", "/api/projects/helios/buy",
                   {"payment_atoms": 750 * 100_000_000}, token=buyer_tok)
    ok.eq(code, 200, "the rest of the allowance is still spendable")

    # --- persistence ------------------------------------------------------
    code, r = _req(base, "GET", "/api/projects/helios")
    ok.eq(code, 200, "project detail is public")
    ok.eq(r["verify"]["script_pubkey"], spk, "detail publishes the address to verify")

    srv.shutdown()
    if state.exists():
        state.unlink()
    return ok.report()


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def eq(self, got, want, what):
        if got == want:
            self.passed += 1
        else:
            self.failed.append("%s: got %r, want %r" % (what, got, want))

    def report(self):
        for f in self.failed:
            print("  FAIL %s" % f)
        print("%d passed, %d failed" % (self.passed, len(self.failed)))
        return 0 if not self.failed else 1


if __name__ == "__main__":
    sys.exit(main())
