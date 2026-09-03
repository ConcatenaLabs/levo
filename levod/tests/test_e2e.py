"""End-to-end drill: stake, sign in, list, lock, buy, reclaim -- against a stub node.

This runs the real HTTP server and the real covenant builder. Only the node is
stubbed, so what it proves is the whole platform except chain propagation: the
sale address is derived by the proven builder, the login signature is really
recovered, and the tier caps really refuse an over-large buy.

Each section runs on its own, so one surprise records a failure and the rest
of the drill still runs.
"""

import json
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import rpc as RPCMOD  # noqa: E402
import signhelper as SH  # noqa: E402

USDX = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"
# A treasury program is a taproot output key, so it has to be a real point on
# the curve: a program that is not one can be paid and never spent, and Levo
# refuses it. This is the x-only key of the secret 0x1111...11.
TREASURY_PROG = "4f355bdcb7cc0af728ef3cceb9615d90684bb5b2ca5f859ab0f0b704075871aa"
TOKEN = "aa" * 32
FAR = int(time.time()) + 10 ** 7          # a close well in the future


class FakeNode:
    """Just enough node to answer everything Levo asks."""

    GENESIS = "ddd11d54c87a2bd94400fd31ce05d8e1110bb4b78e7103f738342086fc4ea92e"

    def __init__(self):
        self.weights = {}
        self.delegated = {}      # controller -> pool signer
        self.utxos = {}          # (txid, vout) -> output dict
        self.height = 12345
        self.unspents = []
        self.rates = {"SBTC": 6400000000000, "USDX": 100000000}
        self.down = False
        self.floor = 4_000_000_000_000     # what this chain requires of a signer
        self.mempool = {}        # txid -> decoded tx
        self.txs = {}            # txid -> decoded tx the node knows but has not got unspent

    def _up(self):
        if self.down:
            raise RPCMOD.RPCError("node down")

    def staker_weights(self):
        self._up()
        return dict(self.weights)

    def controller_weights(self):
        self._up()
        return ({k: {"weight_atoms": v,
                     "delegated": k in self.delegated,
                     "signer": self.delegated.get(k)}
                 for k, v in self.weights.items()}, True)

    def chain_height(self):
        self._up()
        return self.height

    def chain_name(self):
        return "test"

    def median_time(self):
        self._up()
        return int(time.time()) - 600

    def min_relay_fee_atoms_per_kvb(self):
        return 100_000

    def staking_floor(self):
        self._up()
        return self.floor

    def with_timeout(self, seconds):
        return self

    def txout(self, txid, vout, include_mempool=True):
        self._up()
        return self.utxos.get((txid, int(vout)))

    def call(self, method, *params):
        self._up()
        if method == "getfeeexchangerates":
            return dict(self.rates)
        if method == "dumpassetlabels":
            return {"USDX": USDX, "SBTC": "28" * 32}
        if method == "getblockhash":
            return self.GENESIS
        if method == "scantxoutset":
            return {"success": True, "unspents": list(self.unspents)}
        if method == "getblockchaininfo":
            return {"blocks": self.height, "chain": "test"}
        if method == "getrawmempool":
            return list(self.mempool)
        if method == "getrawtransaction":
            return self.mempool.get(params[0]) or self.txs.get(params[0])
        raise RuntimeError("unexpected call %s" % method)


def _req(base, method, path, body=None, token=None, raw=None, headers=None):
    url = base + path
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            text = resp.read()
            try:
                return resp.status, (json.loads(text) if text else {}), resp.headers
            except ValueError:
                return resp.status, {"raw": text.decode(errors="replace")}, resp.headers
    except urllib.error.HTTPError as e:
        text = e.read()
        try:
            return e.code, json.loads(text), e.headers
        except ValueError:
            return e.code, {"raw": text.decode(errors="replace")}, e.headers


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = []
        self._section = ""

    def section(self, name):
        self._section = name

    def ok(self, cond, what, detail=""):
        if cond:
            self.passed += 1
        else:
            self.failed.append("[%s] %s%s" % (self._section, what, (" (%s)" % detail) if detail else ""))

    def eq(self, got, want, what):
        if got == want:
            self.passed += 1
        else:
            self.failed.append("[%s] %s: got %r, want %r" % (self._section, what, got, want))

    def report(self):
        for f in self.failed:
            print("  FAIL %s" % f)
        print("%d passed, %d failed" % (self.passed, len(self.failed)))
        return 0 if not self.failed else 1


class Drill:
    def __init__(self):
        self.state = HERE / "_e2e-state.json"
        if self.state.exists():
            self.state.unlink()
        self.webroot = Path(tempfile.mkdtemp(prefix="levo-e2e-web-"))
        os.environ["LEVOD_STATE"] = str(self.state)
        os.environ["LEVOD_SECRET"] = "test-secret-not-used-in-production"
        os.environ["LEVOD_WEBROOT"] = str(self.webroot)
        # This drill exercises the API, and its webroot starts empty. A levod
        # with no app is a deliberate configuration, and saying so is what
        # keeps health honest for the deployments that do serve one.
        os.environ["LEVOD_API_ONLY"] = "1"
        os.environ["LEVOD_EXPLORER_URL"] = "https://example.test/explorer/"
        os.environ["LEVOD_LINKS"] = json.dumps({"Faucet": "https://example.test/faucet"})
        os.environ["LEVOD_AUTH_PER_MINUTE"] = "100000"     # the drill signs in a lot
        os.environ["LEVOD_OPERATORS"] = SH.pubkey_of(self.issuer_sec)
        os.environ["LEVOD_WRITES_PER_MINUTE"] = "100000"
        os.environ.pop("LEVOD_TIERS", None)
        import server
        self.server_mod = server
        self.node = FakeNode()
        self.app = server.App(node=self.node)
        server.Handler.app = self.app
        from http.server import ThreadingHTTPServer
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.ok = Checker()

    def req(self, method, path, body=None, token=None, **kw):
        code, data, _ = _req(self.base, method, path, body, token, **kw)
        return code, data

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        if self.state.exists():
            self.state.unlink()
        for f in self.webroot.rglob("*"):
            if f.is_file():
                f.unlink()

    # --- the cast -----------------------------------------------------------
    issuer_sec = 0x11abcdef11abcdef11abcdef11abcdef11abcdef11abcdef11abcdef11abcdef
    buyer_sec = 0x22abcdef22abcdef22abcdef22abcdef22abcdef22abcdef22abcdef22abcdef
    issuer_stake_sec = 0x33abcdef33abcdef33abcdef33abcdef33abcdef33abcdef33abcdef33abcdef
    buyer_stake_sec = 0x44abcdef44abcdef44abcdef44abcdef44abcdef44abcdef44abcdef44abcdef

    def sign_in(self, sec, address=None):
        _, ch = self.req("POST", "/api/auth/challenge")
        sig = SH.sign_recoverable(sec, ch["message"])
        body = {"message": ch["message"], "signature": sig}
        if address:
            body["address"] = address
        code, r = self.req("POST", "/api/auth/verify", body)
        self.ok.eq(code, 200, "sign in")
        self.ok.eq(r.get("account"), SH.pubkey_of(sec), "account is the signing key")
        return r.get("token")

    def link_stake(self, token, stake_sec):
        pk = SH.pubkey_of(stake_sec)
        _, ch = self.req("POST", "/api/stake/challenge", {"staker_pubkey": pk}, token=token)
        sig = SH.sign_recoverable(stake_sec, ch["message"])
        code, r = self.req("POST", "/api/stake/link",
                           {"message": ch["message"], "signature": sig, "staker_pubkey": pk},
                           token=token)
        self.ok.eq(code, 200, "link staking key")
        return r


def run(d):
    ok, req, node = d.ok, d.req, d.node
    issuer_stake_pk = SH.pubkey_of(d.issuer_stake_sec)
    buyer_stake_pk = SH.pubkey_of(d.buyer_stake_sec)
    node.weights[issuer_stake_pk] = 25 * 4_000_000_000_000
    node.weights[buyer_stake_pk] = 4_000_000_000_000

    # --- health, config, the HTTP surface ---------------------------------
    ok.section("surface")
    code, r = req("GET", "/api/health")
    ok.eq(code, 200, "health")
    ok.eq(r["node"]["reachable"], True, "node reachable")
    ok.eq(r["node"]["chain"], "test", "health names the chain")
    node.down = True
    code, r = req("GET", "/api/health")
    ok.eq(code, 503, "health is 503 when the node is unreachable")
    ok.eq(r["node"]["reachable"], False, "and says so")
    node.down = False
    code, r = req("GET", "/api/config")
    ok.eq(code, 200, "config")
    ok.eq(r["payment"]["label"], "USDX", "config names the payment asset")
    ok.eq(r["payment"]["asset"], USDX, "and its id")
    ok.eq(r["explorer_url"], "https://example.test/explorer", "and the explorer, without a trailing slash")
    ok.eq(r["links"], {"Faucet": "https://example.test/faucet"}, "and the site links")
    ok.eq(r["stake"]["label"], "tSEQ", "the staking token is tSEQ on chain test")
    ok.eq(r["first_tier_is_chain_floor"], True, "the default first tier is the chain floor")
    code, r = req("GET", "/api/tiers")
    ok.ok("40,000 tSEQ" in r["note"], "the tiers note is built from the table", r["note"])
    ok.eq(r["staking_floor_atoms"], 4_000_000_000_000, "the floor comes from the node")
    ok.eq(r["staking_floor_from_chain"], True, "and says that it did")
    ok.ok("consensus ignores" in r["note"],
          "so the note may say what consensus does", r["note"])
    code, _, h = _req(d.base, "HEAD", "/api/health")
    ok.eq(code, 200, "HEAD works on the API")
    ok.ok(h.get("Content-Length") not in (None, "0"), "HEAD carries the length it would send")
    code, _, h = _req(d.base, "OPTIONS", "/api/health")
    ok.eq(code, 204, "OPTIONS answers")
    ok.ok("GET" in (h.get("Allow") or ""), "with an Allow header")
    code, r = req("PUT", "/api/health")
    ok.eq(code, 405, "an unsupported method is 405")
    code, r = req("GET", "/api/nope")
    ok.eq(code, 404, "an unknown endpoint is 404")
    code, r = req("GET", "/api")
    ok.eq(code, 404, "/api without a slash is not the app")
    code, r = req("GET", "/api/projects/nope")
    ok.eq(code, 404, "an unknown project is 404")
    for raw, what in ((b"[]", "a list body"), (b"5", "a number body"), (b"{", "broken JSON"),
                      (b"\xff\xfe", "non-UTF-8 bytes")):
        code, r = req("POST", "/api/auth/verify", raw=raw)
        ok.eq(code, 400, "%s is a 400, not a 500" % what)
        ok.ok("internal" not in r.get("error", ""), "%s: the error names the problem" % what)
    code, r = req("POST", "/api/auth/verify", raw=b"{}", headers={"Content-Length": "abc"})
    ok.ok(code in (400, 411), "a bad Content-Length is refused")
    code, r, h = _req(d.base, "GET", "/api/health")
    ok.eq(h.get("X-Content-Type-Options"), "nosniff", "security headers are sent")
    ok.eq(h.get("Server"), "levod", "the Server header names no interpreter version")

    # --- the app ------------------------------------------------------------
    ok.section("static")
    (d.webroot / "assets").mkdir()
    (d.webroot / "index.html").write_text("<!doctype html><title>Levo</title>")
    (d.webroot / "assets" / "app.js").write_text("console.log(1)")
    code, _, h = _req(d.base, "GET", "/")
    ok.eq(code, 200, "the app is served")
    ok.ok(h.get("Content-Type", "").startswith("text/html"), "as html")
    ok.ok("frame-ancestors" in (h.get("Content-Security-Policy") or ""), "with a CSP")
    code, _, h = _req(d.base, "GET", "/p/anything")
    ok.eq(code, 200, "deep links fall back to the app")
    code, _, h = _req(d.base, "GET", "/assets/app.js")
    ok.ok("immutable" in (h.get("Cache-Control") or ""), "hashed assets are cached")
    code, r = req("GET", "/../levod/store.py")
    ok.ok(code in (403, 200), "a path outside the webroot is not served as a file")
    ok.ok("import" not in json.dumps(r), "and its content does not leak")

    # --- sign in --------------------------------------------------------------
    ok.section("auth")
    issuer_tok = d.sign_in(d.issuer_sec)
    buyer_tok = d.sign_in(d.buyer_sec)
    # The signed text must be the issued challenge, not any text with a nonce in it.
    _, ch = req("POST", "/api/auth/challenge")
    forged = "Approve a payment of 1 BTC\n" + [l for l in ch["message"].splitlines() if l.startswith("Nonce")][0]
    code, r = req("POST", "/api/auth/verify",
                  {"message": forged, "signature": SH.sign_recoverable(d.buyer_sec, forged)})
    ok.eq(code, 400, "a signature over other text carrying the nonce is refused")
    ok.ok("not the challenge" in r.get("error", ""), "and the reason is stated", r.get("error"))
    _, ch = req("POST", "/api/auth/challenge")
    code, r = req("POST", "/api/auth/verify",
                  {"message": ch["message"], "signature": SH.sign_recoverable(d.buyer_sec, ch["message"])})
    ok.eq(code, 200, "a fresh challenge signs in")
    code, r = req("POST", "/api/auth/verify",
                  {"message": ch["message"], "signature": SH.sign_recoverable(d.buyer_sec, ch["message"])})
    ok.eq(code, 400, "a challenge is single use")
    ok.ok(not ch["message"].endswith("\n"), "the challenge ends without a newline")
    _, ch = req("POST", "/api/auth/challenge")
    trimmed = ch["message"] + "\n"
    code, r = req("POST", "/api/auth/verify",
                  {"message": trimmed, "signature": SH.sign_recoverable(d.buyer_sec, trimmed)})
    ok.eq(code, 200, "a trailing newline added in transit still signs in")
    # Naming the address turns a phantom account into an error.
    import auth as A
    pk = SH.pubkey_of(d.buyer_sec)
    import address as ADDR
    addr = ADDR.from_script_pubkey("0014" + A.hash160(bytes.fromhex(pk)).hex(), "tb")
    tok = d.sign_in(d.buyer_sec, address=addr)
    ok.ok(bool(tok), "signing in with the matching address works")
    _, ch = req("POST", "/api/auth/challenge")
    other = ch["message"].replace("Levo", "Levo ")
    code, r = req("POST", "/api/auth/verify",
                  {"message": ch["message"], "signature": SH.sign_recoverable(d.buyer_sec, other),
                   "address": addr})
    ok.eq(code, 400, "a signature over different bytes is refused when the address is named")
    code, r = req("GET", "/api/me")
    ok.eq(code, 401, "me needs a session")
    code, r = req("GET", "/api/me", token="garbage.token")
    ok.eq(code, 401, "a bad token is 401")

    # --- stake ------------------------------------------------------------------
    ok.section("stake")
    pk = issuer_stake_pk
    _, ch = req("POST", "/api/stake/challenge", {"staker_pubkey": pk}, token=buyer_tok)
    bad = SH.sign_recoverable(d.buyer_stake_sec, ch["message"])   # wrong key signs
    code, r = req("POST", "/api/stake/link",
                  {"message": ch["message"], "signature": bad, "staker_pubkey": pk}, token=buyer_tok)
    ok.eq(code, 400, "cannot link a staking key you do not control")
    st = d.link_stake(issuer_tok, d.issuer_stake_sec)
    ok.eq(st["tier"]["name"], "Founder", "issuer reaches the top tier")
    ok.eq(st["counts_delegated_stake"], True, "stake is read by controller")
    ok.eq(st["tier"]["may_list"], True, "top tier may list")
    pk2 = buyer_stake_pk
    _, ch2 = req("POST", "/api/stake/challenge", {"staker_pubkey": pk2}, token=buyer_tok)
    trimmed = ch2["message"] + "\n"
    code, r = req("POST", "/api/stake/link",
                  {"message": trimmed, "signature": SH.sign_recoverable(d.buyer_stake_sec, trimmed),
                   "staker_pubkey": pk2}, token=buyer_tok)
    ok.eq(code, 200, "a statement with a trailing newline added still links")
    _, ch3 = req("POST", "/api/stake/challenge", {"staker_pubkey": pk2}, token=buyer_tok)
    forged = ch3["message"].replace("Account: " + SH.pubkey_of(d.buyer_sec),
                                    "Account: " + SH.pubkey_of(d.issuer_sec))
    code, r = req("POST", "/api/stake/link",
                  {"message": forged, "signature": SH.sign_recoverable(d.buyer_stake_sec, forged),
                   "staker_pubkey": pk2}, token=buyer_tok)
    ok.eq(code, 400, "a statement naming another account is refused")
    node.delegated[pk2] = "03" + "cc" * 32
    st = d.link_stake(buyer_tok, d.buyer_stake_sec)
    ok.eq(st["tier"]["name"], "Contributor", "buyer reaches tier 1")
    ok.eq(st["keys"][0]["delegated"], True, "with the stake delegated to a pool")
    code, r = req("POST", "/api/stake/unlink", {"staker_pubkey": "02" + "77" * 32}, token=buyer_tok)
    ok.eq(code, 404, "unlinking a key that is not linked is 404")
    code, r = req("POST", "/api/stake/unlink", {"staker_pubkey": pk2}, token=buyer_tok)
    ok.eq(code, 200, "unlink")
    ok.eq(r["tier"]["name"], "Visitor", "and the tier drops")
    st = d.link_stake(buyer_tok, d.buyer_stake_sec)
    ok.eq(st["tier"]["name"], "Contributor", "relinking restores it")

    # --- listing --------------------------------------------------------------
    ok.section("listing")
    total = 1_000_000 * 100_000_000
    terms = {
        "token_asset": TOKEN, "payment_asset": USDX,
        "price_num": 25, "price_den": 100,        # 0.25 USDX per token
        "treasury_prog": TREASURY_PROG, "min_lot": 100_000,
        "close_locktime": FAR, "reclaim_xonly": "22" * 32,
        "total_atoms": total,
    }
    meta = {"slug": "helios", "name": "Helios Grid", "ticker": "HLX",
            "summary": "Solar microgrids, tokenised.",
            "description": "A demonstration listing.",
            "links": {"Website": "https://example.test/helios"}, "decimals": 8}
    code, r = req("POST", "/api/projects", {"project": meta, "terms": terms}, token=buyer_tok)
    ok.eq(code, 403, "tier 1 cannot list a project")
    ok.ok("Founder" in r["error"] and "tSEQ" in r["error"], "the refusal names the listing tier in units", r["error"])
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="x-past"),
                                            "terms": dict(terms, close_locktime=node.height - 1)}, token=issuer_tok)
    ok.eq(code, 400, "a sale closing at a past block is refused")
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="x-past-2"),
                                            "terms": dict(terms, close_locktime=1_000_000_000)}, token=issuer_tok)
    ok.eq(code, 400, "a sale closing at a past date is refused")
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="x-big"),
                                            "terms": dict(terms, close_locktime=5_000_000_000)}, token=issuer_tok)
    ok.eq(code, 400, "a close above 32 bits is refused")
    # A price and a minimum lot that together pay the treasury less than a
    # node will relay: every purchase this sale allows would be refused by the
    # network, so the sale itself is refused.
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="x-dust"),
                                            "terms": dict(terms, price_num=1, price_den=100_000,
                                                          min_lot=100)}, token=issuer_tok)
    ok.eq(code, 400, "a sale whose smallest purchase pays dust is refused")
    ok.ok("minimum lot" in (r.get("error") or ""), "and the refusal says how to fix it",
          r.get("error"))
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="x-lot"),
                                            "terms": dict(terms, min_lot=total + 1)}, token=issuer_tok)
    ok.eq(code, 400, "a minimum lot above the total is refused")
    t2 = dict(terms); del t2["total_atoms"]
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="x-total"), "terms": t2}, token=issuer_tok)
    ok.eq(code, 400, "total_atoms is required")
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="x-asset"),
                                            "terms": dict(terms, payment_asset="bb" * 32)}, token=issuer_tok)
    ok.eq(code, 400, "a sale in another payment asset is refused")
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="x-links",
                                                            links={"Site": "javascript:alert(1)"}),
                                            "terms": terms}, token=issuer_tok)
    ok.eq(code, 400, "a non-http link is refused")
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="x-terms"), "terms": {}}, token=issuer_tok)
    ok.eq(code, 400, "empty terms are a 400 that names the missing field")
    ok.ok("token_asset" in r.get("error", ""), "naming token_asset", r.get("error"))
    code, r = req("POST", "/api/projects", {"project": "abc", "terms": terms}, token=issuer_tok)
    ok.eq(code, 400, "a project that is not an object is a 400")
    code, r = req("POST", "/api/projects", {"project": dict(meta, name=123), "terms": terms}, token=issuer_tok)
    ok.eq(code, 400, "a name that is not text is a 400")
    # A treasury given as a taproot address is decoded to its program.
    import tx as TXMOD
    treasury_addr = ADDR.from_script_pubkey(TXMOD.v1_script_pubkey(TREASURY_PROG).hex(), "tb")
    t3 = dict(terms); del t3["treasury_prog"]; t3["treasury_address"] = treasury_addr
    code, r = req("POST", "/api/projects", {"project": meta, "terms": t3}, token=issuer_tok)
    ok.eq(code, 201, "top tier can list, naming the treasury by address")
    ok.eq(r["project"]["sale"]["terms"]["treasury_prog"], TREASURY_PROG, "which decodes to the program")
    ok.eq(r["project"]["links"], {"Website": "https://example.test/helios"}, "links are kept")
    spk = r["lock"]["script_pubkey"]
    import covenant as C
    published = r["project"]["sale"]["terms"]
    ok.eq(C.derive(C.SaleTerms.from_json(published)).spk_hex, spk,
          "sale address is reproducible from published terms")
    ok.eq((published["price_num"], published["price_den"]), (1, 4), "the price is stored in lowest terms")
    ok.eq(r["lock"]["price_reduced"], True, "the response says the price was reduced")
    ok.eq(r["lock"]["amount"], "1,000,000 HLX", "the lock instructions say the amount in units")
    code, r = req("POST", "/api/projects", {"project": meta, "terms": terms}, token=issuer_tok)
    ok.eq(code, 400, "a duplicate page name is refused")
    code, r = req("POST", "/api/projects",
                  {"project": dict(meta, slug="pretender", ticker="USDX"),
                   "terms": dict(terms, min_lot=terms["min_lot"] + 3)}, token=issuer_tok)
    ok.eq(code, 400, "a listing cannot take the ticker sales are priced in")
    ok.ok("ticker" in (r.get("error") or ""), "saying why", r.get("error"))
    code, r = req("GET", "/api/me/projects", token=issuer_tok)
    ok.eq([p["slug"] for p in r["projects"]], ["helios"], "the issuer sees their project")
    ok.eq(r["total"], 1, "and how many there are in all")
    ok.ok(r["projects"][0]["lock"] is not None, "with lock instructions while it is a draft")
    code, r = req("PATCH", "/api/projects/helios", {"summary": "Edited.", "links": {}}, token=buyer_tok)
    ok.eq(code, 403, "a stranger cannot edit a project")
    code, r = req("PATCH", "/api/projects/helios", {"summary": "Edited.", "links": {}}, token=issuer_tok)
    ok.eq(code, 200, "the issuer can edit copy")
    code, r = req("PATCH", "/api/projects/helios", {"decimals": 2}, token=issuer_tok)
    ok.eq(code, 400, "but not the decimals, which would reprice every amount")
    ok.ok("Withdraw" in (r.get("error") or ""), "and is told what to do instead", r.get("error"))
    code, r = req("PATCH", "/api/projects/helios", {"decimals": 8}, token=issuer_tok)
    ok.eq(code, 200, "while sending the decimals it already has is not a change")
    code, r = req("PATCH", "/api/projects/helios", {"issuance_txid": "not-a-txid"}, token=issuer_tok)
    ok.eq(code, 400, "an issuance that is not a transaction id is refused")
    code, r = req("PATCH", "/api/projects/helios", {"issuance_txid": "b7" * 32}, token=issuer_tok)
    ok.eq(code, 200, "and a real one is published")
    ok.eq(r["issuance_txid"], "b7" * 32, "as itself, for a reader to follow")
    ok.eq(r["summary"], "Edited.", "and the edit lands")
    # A second listing has to differ in something the address is made of.
    # Identical terms derive one covenant, and two sales sharing one could not
    # be told apart on chain.
    code, r = req("POST", "/api/projects",
                  {"project": dict(meta, slug="same-terms"), "terms": terms},
                  token=issuer_tok)
    ok.eq(code, 400, "a listing that derives an existing sale address is refused")
    ok.ok("same sale address" in (r.get("error") or ""), "with the reason")
    second = dict(terms, min_lot=terms["min_lot"] + 1)
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="to-withdraw"), "terms": second}, token=issuer_tok)
    ok.eq(code, 201, "a second listing")
    code, r = req("DELETE", "/api/projects/to-withdraw", token=buyer_tok)
    ok.eq(code, 403, "a stranger cannot withdraw it")
    code, r = req("DELETE", "/api/projects/to-withdraw", token=issuer_tok)
    ok.eq(code, 200, "the issuer withdraws an unfunded draft")
    code, r = req("GET", "/api/projects/to-withdraw")
    ok.eq(code, 404, "and it is gone")
    for i in range(3):
        code, r = req("POST", "/api/projects",
                      {"project": dict(meta, slug="draft-%d" % i),
                       "terms": dict(terms, min_lot=terms["min_lot"] + 10 + i)},
                      token=issuer_tok)
    ok.eq(code, 400, "a fourth unfunded draft is refused (helios is still a draft)")
    ok.ok("waiting to be funded" in r["error"], "with the reason", r["error"])
    for i in range(2):
        req("DELETE", "/api/projects/draft-%d" % i, token=issuer_tok)

    # --- not investable until locked --------------------------------------
    ok.section("lock")
    code, r = req("POST", "/api/projects/helios/buy", {"token_atoms": 100_000_000}, token=buyer_tok)
    ok.eq(code, 400, "cannot buy before the tokens are locked")
    node.utxos[("cc" * 32, 0)] = {"scriptPubKey": {"hex": "51200000"}, "asset": TOKEN, "valueatoms": total}
    code, r = req("POST", "/api/projects/helios/lock", {"txid": "cc" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 400, "a lock at the wrong address is refused")
    node.utxos[("dd" * 32, 0)] = {"scriptPubKey": {"hex": spk}, "asset": "bb" * 32, "valueatoms": total}
    code, r = req("POST", "/api/projects/helios/lock", {"txid": "dd" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 400, "a lock holding the wrong asset is refused")
    node.utxos[("ee" * 32, 0)] = {"scriptPubKey": {"hex": spk}, "asset": TOKEN, "valueatoms": total - 1}
    code, r = req("POST", "/api/projects/helios/lock", {"txid": "ee" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 400, "a short lock is refused")
    ok.ok("HLX" in r["error"], "in the token's units", r["error"])
    node.utxos[("e2" * 32, 0)] = {"scriptPubKey": {"hex": spk}, "asset": TOKEN, "valueatoms": total + 1}
    code, r = req("POST", "/api/projects/helios/lock", {"txid": "e2" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 400, "an over-sized lock is refused too: the allocation is what was published")
    code, r = req("POST", "/api/projects/helios/lock", {"txid": "ff" * 32}, token=issuer_tok)
    ok.eq(code, 400, "a lock without a vout is a 400, not a crash")
    code, r = req("POST", "/api/projects/helios/lock", {}, token=issuer_tok)
    ok.eq(code, 400, "with nothing at the address, an unnamed lock is not found")
    ok.ok("confirmed block" in r["error"], "and the reason says to wait or name the outpoint", r["error"])
    # The real lock, found by scanning rather than named.
    node.utxos[("ff" * 32, 0)] = {"scriptPubKey": {"hex": spk}, "asset": TOKEN, "valueatoms": total, "confirmations": 1}
    node.unspents = [{"txid": "ff" * 32, "vout": 0, "scriptPubKey": spk, "amount": total / 1e8,
                      "asset": TOKEN, "height": node.height}]
    code, r = req("POST", "/api/projects/helios/lock", {}, token=issuer_tok)
    ok.eq(code, 200, "the lock is found on chain without naming it")
    ok.eq(r["sale"]["status"], "live", "sale goes live once locked")
    ok.eq(r["sale"]["funding"]["txid"], "ff" * 32, "at the outpoint the scan found")
    code, r = req("POST", "/api/projects/helios/lock", {"txid": "ff" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 400, "a second lock on a live sale is refused")
    code, r = req("POST", "/api/projects/helios/lock", {"txid": "ff" * 32, "vout": 0}, token=buyer_tok)
    ok.eq(code, 403, "a stranger cannot confirm someone else's lock")
    code, r = req("DELETE", "/api/projects/helios", token=issuer_tok)
    ok.eq(code, 400, "a funded sale cannot be withdrawn")

    # --- buying -----------------------------------------------------------
    ok.section("buy")
    code, plan = req("POST", "/api/projects/helios/buy", {"token_atoms": 1_000 * 100_000_000}, token=buyer_tok)
    ok.eq(code, 200, "tier 1 buy within cap")
    ok.eq(plan["payment_atoms"], 250 * 100_000_000, "1,000 tokens cost 250 USDX")
    ok.eq(plan["required_outputs"][0]["index"], 0, "treasury credit at output 2k")
    ok.eq(plan["required_outputs"][1]["index"], 1, "remainder re-rests at 2k+1")
    ok.eq(plan["required_outputs"][1]["script_pubkey"], spk, "remainder returns to the identical covenant")
    ok.eq(plan["cap"]["enforced_by"], "levo", "caps are labelled as policy")
    ok.eq(len(plan["covenant"]["witness"]), 2, "sell witness is leaf + control block")
    ok.ok(plan["fee"]["suggested_atoms"] and plan["fee"]["suggested_atoms"] > plan["fee"]["min_atoms"] > 0,
          "a fee is suggested from the node's floor", plan["fee"])
    code, plan_s = req("POST", "/api/projects/helios/buy", {"token_atoms": str(1_000 * 100_000_000)}, token=buyer_tok)
    ok.eq(plan_s["payment_atoms"], plan["payment_atoms"], "an atom count as a decimal string is accepted")
    code, r = req("POST", "/api/projects/helios/buy", {"token_atoms": 100_000 * 100_000_000}, token=buyer_tok)
    ok.eq(code, 409, "a buy beyond the tier cap is refused")
    ok.ok("USDX" in r["error"] and "atoms" not in r["error"], "in units, not atoms", r["error"])
    code, r = req("POST", "/api/projects/helios/buy", {"token_atoms": 10}, token=buyer_tok)
    ok.eq(code, 400, "a buy below the minimum lot is refused")
    ok.ok("HLX" in r["error"], "naming the token", r["error"])
    code, r = req("POST", "/api/projects/helios/buy", {"token_atoms": "abc"}, token=buyer_tok)
    ok.eq(code, 400, "a non-number is refused with a reason")
    ok.ok("whole number" in r["error"], "that says what a number is", r["error"])
    code, r = req("POST", "/api/projects/helios/buy", {"token_atoms": 100_000_000})
    ok.eq(code, 401, "buying requires a signed-in wallet")
    node.down = True
    code, r = req("POST", "/api/projects/helios/buy", {"token_atoms": 100_000_000}, token=buyer_tok)
    ok.ok(code in (400, 502), "with the node down, a buy is refused rather than quoted", code)
    node.down = False

    # --- building the transaction that settles it -------------------------
    ok.section("transaction")
    buyer_addr = ADDR.from_script_pubkey("0014" + "cc" * 20, "tb")
    # The fee comes from what the plan advised, which is what a client does.
    # A figure below the node's relay floor is refused here rather than by
    # every node the buyer offers the signed transaction to.
    code, planned = req("POST", "/api/projects/helios/buy",
                        {"token_atoms": 100 * 100_000_000}, token=buyer_tok)
    fee_atoms = planned["fee"]["suggested_atoms"]
    ok.ok(fee_atoms and fee_atoms >= planned["fee"]["min_atoms"],
          "the plan advises a fee at or above the node's floor")
    body = {"token_atoms": 100 * 100_000_000,
            "buyer": {"token_address": buyer_addr, "change_address": buyer_addr,
                      "inputs": [{"txid": "77" * 32, "vout": 0}], "fee_atoms": fee_atoms}}
    code, r = req("POST", "/api/projects/helios/transaction", body, token=issuer_tok)
    ok.eq(code, 400, "an input that is not on chain is refused")
    node.utxos[("77" * 32, 0)] = {"scriptPubKey": {"hex": "0014" + "cc" * 20}, "asset": USDX,
                                  "valueatoms": 10_000 * 100_000_000}
    code, built = req("POST", "/api/projects/helios/transaction", body, token=issuer_tok)
    ok.eq(code, 200, "a funded purchase builds a transaction")
    ok.ok(len(built["unsigned_tx_hex"]) > 200, "which is a real transaction")
    ok.ok(built["pset"] and built["pset"].startswith("cHNldP8"), "and a PSET for a browser wallet")
    ok.eq(built["inputs"][0]["role"], "the sale covenant", "covenant is input 0")
    ok.ok("none" in built["inputs"][0]["signing"], "and needs no signature")
    ok.eq(built["outputs"][0]["role"], "treasury credit, checked by the covenant", "the treasury is paid at output 0")
    ok.eq(built["outputs"][1]["script_pubkey"], spk, "the remainder re-rests at the sale address")
    ok.eq(built["outputs"][2]["script_pubkey"], "0014" + "cc" * 20, "the tokens go to the decoded address")
    ok.eq(built["outputs"][2]["role"], "your tokens", "labelled as the buyer's tokens")
    ok.ok(built["vsize_estimate"] <= planned["fee"]["vsize_estimate"],
          "and sized the transaction generously rather than short",
          "%s vs advised %s" % (built["vsize_estimate"], planned["fee"]["vsize_estimate"]))
    for bad, why in ((0, "a fee of nothing"), (1000, "a fee below the relay floor")):
        code, r = req("POST", "/api/projects/helios/transaction",
                      dict(body, buyer=dict(body["buyer"], fee_atoms=bad)), token=issuer_tok)
        ok.eq(code, 400, "%s is refused" % why)
        ok.ok("fee" in (r.get("error") or "").lower(), "with a message about the fee", r.get("error"))
    code, r = req("POST", "/api/projects/helios/transaction",
                  dict(body, buyer=dict(body["buyer"], fee_asset="USDX")), token=issuer_tok)
    ok.eq(code, 200, "the fee asset may be given as the label the wallet shows")
    code, r = req("POST", "/api/projects/helios/transaction",
                  dict(body, buyer=dict(body["buyer"], token_address=None,
                                        token_script_pubkey="5320" + "cc" * 32)),
                  token=issuer_tok)
    ok.eq(code, 400, "an anyone-can-spend destination is refused")
    ok.ok("anyone" in (r.get("error") or ""), "saying why", r.get("error"))
    code, r = req("POST", "/api/projects/helios/transaction",
                  {"token_atoms": body["token_atoms"],
                   "buyer": {"token_address": buyer_addr, "inputs": body["buyer"]["inputs"],
                             "fee_atoms": fee_atoms}}, token=issuer_tok)
    ok.eq(code, 200, "leaving the change address blank sends change where the tokens go")
    ok.ok(any(o["script_pubkey"] == "0014" + "cc" * 20 and o["role"] == "your change"
              for o in r["outputs"]), "with a change output at that address")
    body_dup = dict(body, buyer=dict(body["buyer"], inputs=[{"txid": "77" * 32, "vout": 0}] * 2))
    code, r = req("POST", "/api/projects/helios/transaction", body_dup, token=issuer_tok)
    ok.eq(code, 400, "an input listed twice is refused")
    many = [{"txid": "77" * 32, "vout": i} for i in range(40)]
    code, r = req("POST", "/api/projects/helios/transaction", dict(body, buyer=dict(body["buyer"], inputs=many)),
                  token=issuer_tok)
    ok.eq(code, 400, "forty inputs are refused before the node is asked about any")
    ok.ok("at most" in r["error"], "with the limit named", r["error"])
    code, r = req("POST", "/api/projects/helios/transaction",
                  dict(body, buyer=dict(body["buyer"], token_address="tsqb1qqfw9w0z4lq8lqm0ma5m0q3e6rnwm6wqhhk9n4l0u3f6yg9d3rxkcpxyzhsrkcqnfcd0")),
                  token=issuer_tok)
    ok.eq(code, 400, "a confidential destination is refused")
    ok.ok("confidential" in r["error"], "with the reason", r["error"])
    code, r = req("POST", "/api/projects/helios/transaction",
                  dict(body, buyer=dict(body["buyer"], fee_asset="ee" * 32)), token=issuer_tok)
    ok.eq(code, 400, "a fee in an unaccepted asset is refused")
    ok.ok("open fee market" in r["error"], "and explains the chain's fee market")
    node.utxos[("78" * 32, 0)] = {"scriptPubKey": {"hex": "0014" + "cc" * 20}, "asset": USDX,
                                  "valueatoms": 10_000 * 100_000_000, "valuecommitment": "08" + "ab" * 32}
    code, r = req("POST", "/api/projects/helios/transaction",
                  dict(body, buyer=dict(body["buyer"], inputs=[{"txid": "78" * 32, "vout": 0}])), token=issuer_tok)
    ok.eq(code, 400, "a confidential input is refused")
    ok.ok("confidential" in r["error"], "and says why")
    code, r = req("POST", "/api/projects/helios/transaction",
                  dict(body, buyer=dict(body["buyer"], inputs=[{"txid": "77" * 32}])), token=issuer_tok)
    ok.eq(code, 400, "an input without a vout is a 400")
    code, r = req("POST", "/api/projects/helios/transaction", dict(body, buyer="abc"), token=issuer_tok)
    ok.eq(code, 400, "a buyer that is not an object is a 400")
    # The covenant outpoint has been spent by someone: no transaction is built.
    saved = node.utxos.pop(("ff" * 32, 0))
    code, r = req("POST", "/api/projects/helios/transaction", body, token=issuer_tok)
    ok.eq(code, 400, "a purchase against a spent outpoint is refused")
    ok.ok("moved" in r["error"], "and says the sale moved", r["error"])
    node.utxos[("ff" * 32, 0)] = saved

    # --- recording the purchase ---------------------------------------------
    ok.section("confirm")
    treasury_spk = "5120" + TREASURY_PROG
    node.utxos[("ab" * 32, 0)] = {"scriptPubKey": {"hex": treasury_spk}, "asset": USDX,
                                  "valueatoms": 250 * 100_000_000}
    code, r = req("POST", "/api/projects/helios/confirm",
                  {"txid": "ab" * 32, "token_atoms": 1_000 * 100_000_000, "payment_atoms": 250 * 100_000_000},
                  token=buyer_tok)
    ok.eq(code, 200, "purchase recorded")
    ok.eq(r["recorded"], True, "the allocation ledger took it")
    ok.eq(r["treasury_payment_verified"], True, "and checked that the transaction really paid this sale's treasury")
    ok.eq(r["committed_atoms"], 250 * 100_000_000, "the account's commitment")
    code, r = req("POST", "/api/projects/helios/buy", {"payment_atoms": 800 * 100_000_000}, token=buyer_tok)
    ok.eq(code, 409, "the tier cap is cumulative across purchases")
    code, r = req("POST", "/api/projects/helios/buy", {"payment_atoms": 750 * 100_000_000}, token=buyer_tok)
    ok.eq(code, 200, "the rest of the allowance is still spendable")
    for bad, what in (({"txid": "ab" * 32, "token_atoms": 1, "payment_atoms": -5_000_000_000_000}, "a negative payment"),
                      ({"txid": "", "token_atoms": 1, "payment_atoms": 1}, "no txid"),
                      ({"token_atoms": 1, "payment_atoms": 1}, "a missing txid"),
                      ({"txid": [1], "token_atoms": 1, "payment_atoms": 1}, "a txid that is not text"),
                      ({"txid": "ab" * 32, "token_atoms": 0, "payment_atoms": 1}, "no tokens")):
        code, r = req("POST", "/api/projects/helios/confirm", bad, token=buyer_tok)
        ok.eq(code, 400, "%s is refused" % what)
    code, r = req("GET", "/api/me/positions", token=buyer_tok)
    ok.eq(r["positions"][0]["committed_atoms"], 250 * 100_000_000, "the ledger never went down")
    node.utxos[("cc" * 32, 0)] = {"scriptPubKey": {"hex": "5120" + "99" * 32}, "asset": USDX, "valueatoms": 1}
    code, r = req("POST", "/api/projects/helios/confirm", {"txid": "cc" * 32, "token_atoms": 1, "payment_atoms": 1},
                  token=buyer_tok)
    ok.eq(code, 400, "a transaction paying another treasury is refused")
    code, r = req("POST", "/api/projects/helios/confirm",
                  {"txid": "d9" * 32, "token_atoms": 100_000, "payment_atoms": 1}, token=buyer_tok)
    ok.eq(code, 400, "a purchase the node has never seen is refused")
    ok.ok("not seen" in (r.get("error") or ""), "saying so", r.get("error"))
    ok.ok("wait a few seconds" in (r.get("error") or ""),
          "and saying that a just-broadcast purchase may not have arrived yet",
          r.get("error"))
    ok.ok("on chain either way" in (r.get("error") or ""),
          "and that the purchase itself is safe regardless", r.get("error"))
    # A purchase the node knows about, whose treasury output has since been
    # spent: it cannot be checked, and it still happened.
    node.txs["e1" * 32] = {"txid": "e1" * 32, "vin": [], "vout": []}
    code, r = req("POST", "/api/projects/helios/confirm",
                  {"txid": "e1" * 32, "token_atoms": 100_000, "payment_atoms": 1}, token=buyer_tok)
    ok.eq(code, 200, "an unverifiable purchase the node knows is still recorded")
    ok.eq(r["treasury_payment_verified"], None, "and says it could not be checked")
    ok.eq(r["purchase"]["payment_atoms"], 25_000, "but never for less than the covenant's price for the tokens")
    code, r = req("POST", "/api/projects/helios/confirm",
                  {"txid": "e1" * 32, "token_atoms": 100_000, "payment_atoms": 1}, token=buyer_tok)
    ok.eq(code, 200, "recording the same purchase again is accepted")
    ok.eq(r.get("already_recorded"), True, "and says it was already there")
    code, r = req("GET", "/api/me/positions", token=buyer_tok)
    ok.ok(r["total"] >= 1, "positions say how many there are", r["total"])
    ok.ok(r["limit"] and r["limit"] <= 200, "and come back a page at a time", r["limit"])
    pos = r["positions"][0]
    ok.eq(pos["slug"], "helios", "positions list the sale")
    ok.eq(len(pos["purchases"]), 2, "with both purchases")
    ok.eq(pos["tokens_atoms"], 1_000 * 100_000_000 + 100_000, "and the tokens bought")
    code, r = req("GET", "/api/projects/helios")
    ok.eq(r["sale"]["buyers"], 1, "the sale counts its buyers")

    # --- rails ------------------------------------------------------------
    ok.section("rails")
    code, r = req("GET", "/api/rails")
    rails = {x["id"]: x for x in r["rails"]}
    ok.eq(rails["usdx"]["asset"], USDX, "the USDX rail names its asset")
    ok.eq(rails["btc"]["steps"], 2, "and BTC settles in two")
    code, plan = req("POST", "/api/projects/helios/buy", {"token_atoms": 100 * 100_000_000, "rail": "BTC"},
                     token=issuer_tok)
    ok.eq(code, 200, "a BTC-priced quote, rail named in any case")
    ok.eq(plan["quote"]["send_sats"], 39063, "25 USDX at 64,000 USDX/BTC is 39062.5 sats, rounded up")
    ok.ok("0.00039063 BTC" in plan["quote"]["steps"][0], "the steps are in units", plan["quote"]["steps"][0])
    node.rates = {"USDX": 100000000}
    d.app.rails.rate_source._cached = None
    code, r = req("GET", "/api/rails")
    ok.eq({x["id"]: x["available"] for x in r["rails"]}["btc"], False, "without a bitcoin price the BTC rail is unavailable")
    code, r = req("POST", "/api/projects/helios/buy", {"token_atoms": 100 * 100_000_000, "rail": "btc"}, token=issuer_tok)
    ok.eq(code, 400, "and a BTC quote is refused")
    node.rates = {"SBTC": 6400000000000, "USDX": 100000000}
    d.app.rails.rate_source._cached = None

    # --- reclaim ----------------------------------------------------------
    ok.section("reclaim")
    code, r = req("POST", "/api/projects/helios/reclaim", {}, token=buyer_tok)
    ok.eq(code, 403, "only the issuer may reclaim")
    code, r = req("POST", "/api/projects/helios/reclaim", {}, token=issuer_tok)
    ok.eq(code, 400, "a reclaim before the close is refused")
    ok.ok("not closed" in r["error"], "and explains that the covenant would reject it", r["error"])
    # A height-closed sale, past its close.
    code, r = req("POST", "/api/projects", {"project": dict(meta, slug="closing", links={}),
                                            "terms": dict(terms, close_locktime=node.height + 2, reclaim_xonly="44" * 32)},
                  token=issuer_tok)
    ok.eq(code, 201, "a height-closed listing")
    spk2 = r["lock"]["script_pubkey"]
    node.utxos[("f2" * 32, 0)] = {"scriptPubKey": {"hex": spk2}, "asset": TOKEN, "valueatoms": total, "confirmations": 1}
    code, r = req("POST", "/api/projects/closing/lock", {"txid": "f2" * 32, "vout": 0}, token=issuer_tok)
    ok.eq(code, 200, "locked")
    node.height += 5
    code, r = req("GET", "/api/projects/closing")
    ok.eq(r["sale"]["status"], "closed", "past its close the sale reads closed")
    code, r = req("POST", "/api/projects/closing/reclaim", {"fee_inputs": [{"txid": "77" * 32, "vout": 0}], "fee_atoms": 200_000},
                  token=issuer_tok)
    ok.eq(code, 400, "a reclaim needs a destination")
    code, r = req("POST", "/api/projects/closing/reclaim",
                  {"destination_address": buyer_addr, "fee_inputs": [{"txid": "77" * 32, "vout": 0}], "fee_atoms": 200_000},
                  token=issuer_tok)
    ok.eq(code, 400, "change from the fee inputs needs somewhere of its own to go")
    ok.ok("change_address" in (r.get("error") or ""), "and the refusal says which field",
          r.get("error"))
    code, r = req("POST", "/api/projects/closing/reclaim",
                  {"destination_address": buyer_addr, "change_address": ADDR.from_script_pubkey("0014" + "ee" * 20, "tb"),
                   "fee_inputs": [{"txid": "77" * 32, "vout": 0}], "fee_atoms": 200_000},
                  token=issuer_tok)
    ok.eq(code, 200, "after the close a reclaim builds")
    ok.ok("sighash" in r and "leaf" in r and "control_block" in r and "signature" not in r,
          "it hands back what the project signs, and no signature")
    ok.eq(r["locktime"], node.height - 3, "with the covenant's locktime")
    code, p = req("GET", "/api/projects/closing")
    ok.eq(p["sale"]["reclaim_txids"], [r["txid"]], "and the sale remembers the reclaim it built")

    # --- the watcher and the app --------------------------------------------
    ok.section("misc")
    code, r = req("GET", "/api/watcher")
    ok.eq(code, 200, "the watcher reports itself")
    code, r = req("GET", "/api/projects/helios")
    ok.ok(r["address"].startswith("tb1p"), "a sale publishes a bech32m address, not just a scriptPubKey")
    ok.eq(r["verify"]["sell_leaf"], C.derive(C.SaleTerms.from_json(r["sale"]["terms"])).sell_leaf.hex(),
          "and its leaves, so a client can keep them")
    ok.eq(r["node_reachable"], True, "and whether the node answered")

    # --- limits and the headers a client acts on ---------------------------
    ok.section("limits")
    _, _, h = _req(d.base, "GET", "/api/config")
    ok.ok("max-age" in (h.get("Cache-Control") or ""),
          "the config answer may be held for a moment", h.get("Cache-Control"))
    _, _, h = _req(d.base, "GET", "/api/projects")
    ok.eq(h.get("Cache-Control"), "no-store", "the board is never cached")
    saved = d.app.read_limit
    d.app.read_limit = d.server_mod.RateLimit(per_minute=1)
    req("GET", "/api/projects")
    code, r, h = _req(d.base, "GET", "/api/projects")
    ok.eq(code, 429, "a caller reading in a loop is slowed down")
    ok.eq(h.get("Retry-After"), "60", "and told how long to wait")
    code, _, _ = _req(d.base, "GET", "/api/health")
    ok.eq(code, 200, "while health is never the thing that trips the limit")
    d.app.read_limit = saved

    # --- which of a wallet's outputs a covenant can actually spend ---------
    ok.section("outputs")
    node.utxos[("b1" * 32, 0)] = {"scriptPubKey": {"hex": "0014" + "cc" * 20},
                                  "assetcommitment": "0a" * 33,
                                  "valuecommitment": "08" * 33}
    code, r = req("POST", "/api/outputs/check",
                  {"outputs": [{"txid": "77" * 32, "vout": 0},
                               {"txid": "b1" * 32, "vout": 0},
                               {"txid": "b2" * 32, "vout": 7}]}, token=buyer_tok)
    ok.eq(code, 200, "a signed-in caller can ask about its own outputs")
    rows = {(x["txid"], x["vout"]): x for x in r["outputs"]}
    ok.eq(rows[("77" * 32, 0)]["spendable"], True, "an explicit output can be spent")
    ok.eq(rows[("77" * 32, 0)]["asset"], USDX, "and says what it holds")
    ok.eq(rows[("b1" * 32, 0)]["spendable"], False, "a confidential one cannot")
    ok.ok("confidential" in rows[("b1" * 32, 0)]["why"], "and says why",
          rows[("b1" * 32, 0)]["why"])
    ok.eq(rows[("b2" * 32, 7)]["spendable"], False, "nor one that is not there")
    code, r = req("POST", "/api/outputs/check", {"outputs": [{"txid": "77" * 32, "vout": 0}]})
    ok.eq(code, 401, "and it takes a session")

    # --- the board a visitor lands on --------------------------------------
    ok.section("board")
    code, r = req("GET", "/api/projects")
    ok.eq(code, 200, "the board answers")
    ok.ok(r["total"] >= 2, "and says how many listings there are", r["total"])
    ok.eq(len(r["projects"]), r["total"], "with all of them when no page is asked for")
    code, r = req("GET", "/api/projects?status=open")
    ok.ok(all(p["sale"]["status"] in ("live", "partial") for p in r["projects"]),
          "the open filter returns only sales that can be bought")
    code, r = req("GET", "/api/projects?status=finished")
    ok.ok(all(p["sale"]["status"] in ("sold_out", "closed", "reclaimed") for p in r["projects"]),
          "and the finished filter only ones that cannot")
    code, r = req("GET", "/api/projects?limit=1&offset=0")
    ok.eq(len(r["projects"]), 1, "a page holds what was asked for")
    first = r["projects"][0]["slug"]
    code, r = req("GET", "/api/projects?limit=1&offset=1")
    ok.ok(r["projects"] and r["projects"][0]["slug"] != first, "and the next page moves on")
    code, r = req("GET", "/api/projects?q=helios")
    ok.ok("helios" in [p["slug"] for p in r["projects"]],
          "a search finds a listing by name")
    code, r = req("GET", "/api/projects?q=closing")
    ok.eq([p["slug"] for p in r["projects"]], ["closing"], "and by its page name")
    code, r = req("GET", "/api/projects?q=nothing-is-called-this")
    ok.eq(r["projects"], [], "and finds nothing when there is nothing")
    code, r = req("GET", "/api/projects?status=nonsense")
    ok.eq(code, 400, "an unknown filter is refused rather than ignored")
    code, r = req("GET", "/api/projects?limit=abc")
    ok.eq(code, 400, "and so is a page size that is not a number")

    # --- what an operator can and cannot do --------------------------------
    ok.section("operator")
    code, r = req("POST", "/api/projects/helios/flag", {"hidden": True}, token=buyer_tok)
    ok.eq(code, 403, "an ordinary account cannot flag a listing")
    code, r = req("POST", "/api/projects/helios/flag",
                  {"hidden": True, "notice": "Under review."}, token=issuer_tok)
    ok.eq(code, 200, "an operator can")
    ok.ok("covenant" in r["reaches"], "and is told how far that reaches", r["reaches"])
    code, r = req("GET", "/api/projects")
    ok.ok("helios" not in [p["slug"] for p in r["projects"]], "a flagged listing leaves the board")
    code, r = req("GET", "/api/projects/helios")
    ok.eq(code, 200, "but its page still answers")
    ok.eq(r["notice"], "Under review.", "carrying the operator's notice")
    code, r = req("POST", "/api/projects/helios/flag", {"hidden": False}, token=issuer_tok)
    ok.eq(code, 200, "and it can be put back")
    code, r = req("GET", "/api/projects")
    ok.ok("helios" in [p["slug"] for p in r["projects"]], "back on the board")

    # --- what each path takes ----------------------------------------------
    ok.section("verbs")
    code, r, h = _req(d.base, "OPTIONS", "/api/health")
    ok.eq(code, 204, "OPTIONS answers")
    ok.eq(h.get("Allow"), "GET, HEAD, OPTIONS", "with what that path takes")
    code, r, h = _req(d.base, "OPTIONS", "/api/projects")
    ok.eq(h.get("Allow"), "GET, POST, HEAD, OPTIONS", "and a different list elsewhere")
    code, r, h = _req(d.base, "DELETE", "/api/health")
    ok.eq(code, 405, "a verb a path does not take is 405, not 404")
    ok.ok("GET" in (h.get("Allow") or ""), "with the verbs it does take", h.get("Allow"))
    code, r, _ = _req(d.base, "GET", "/api/no-such-thing")
    ok.eq(code, 404, "while a path that does not exist is still 404")
    # PUT went through a hand-written reply that named every verb for every
    # path, contradicting the OPTIONS answer for the same URL and skipping the
    # site's own headers.
    code, r, h = _req(d.base, "PUT", "/api/health")
    ok.eq(code, 405, "PUT is refused")
    ok.eq(h.get("Allow"), "GET, HEAD, OPTIONS", "with the same list OPTIONS gives")
    ok.eq(h.get("X-Content-Type-Options"), "nosniff",
          "and the site's own headers on it")
    code, r, h = _req(d.base, "POST", "/index.html")
    ok.eq(code, 405, "a static file takes no POST")
    ok.eq(h.get("Allow"), "GET, HEAD, OPTIONS", "and says so")

    # --- the ledger an issuer can read -------------------------------------
    ok.section("ledger")
    code, r = req("GET", "/api/projects/helios/purchases", token=issuer_tok)
    ok.eq(code, 200, "the issuer can read what Levo recorded for its own sale")
    ok.ok(len(r["purchases"]) >= 1, "with the purchases in it", len(r["purchases"]))
    ok.ok(r["total"] >= len(r["purchases"]), "and a total to page through", r["total"])
    ok.ok(all("account" in e and "txid" in e for e in r["purchases"]),
          "each naming the account and the transaction")
    ok.ok("chain" in r["what_this_is"], "and saying what it is not")
    code, r = req("GET", "/api/projects/helios/purchases", token=buyer_tok)
    ok.eq(code, 403, "a buyer cannot read the whole ledger")
    code, r = req("GET", "/api/me", token=issuer_tok)
    ok.eq(r.get("operator"), True, "an operator is told they are one")
    code, r = req("GET", "/api/me", token=buyer_tok)
    ok.eq(r.get("operator"), False, "and an ordinary account is told it is not")

    # --- the ledger only records purchases that could have happened --------
    ok.section("ledger")
    for body, why in (
            ({"txid": "d1" * 32, "token_atoms": 1, "payment_atoms": 1},
             "a purchase below the covenant's minimum lot"),
            ({"txid": "d2" * 32, "token_atoms": total * 2, "payment_atoms": 1},
             "a purchase larger than the sale ever held")):
        code, r = req("POST", "/api/projects/helios/confirm", body, token=buyer_tok)
        ok.eq(code, 400, "%s is refused" % why)
    code, r = req("POST", "/api/projects/helios/confirm",
                  {"txid": "e1" * 32, "token_atoms": 100_000, "payment_atoms": 1},
                  token=issuer_tok)
    ok.eq(code, 400, "a purchase already recorded by another account is refused")
    ok.ok("another account" in (r.get("error") or ""), "saying so", r.get("error"))

    # --- the statement a wallet is asked to sign ---------------------------
    ok.section("login")
    code, ch = req("POST", "/api/auth/challenge")
    ok.ok("Site: http" in ch["message"], "the login statement names the site it signs in to",
          ch["message"].splitlines()[:2])


def main():
    d = Drill()
    try:
        run(d)
    except Exception:
        d.ok.failed.append("the drill raised:\n" + traceback.format_exc())
    finally:
        d.close()
    return d.ok.report()


if __name__ == "__main__":
    sys.exit(main())
