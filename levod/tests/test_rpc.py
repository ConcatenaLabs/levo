"""The node client against a fake JSON-RPC server: credentials, error
surfacing, exact amounts, and the stake-view fallbacks."""

import json
import sys
import tempfile
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import rpc as R  # noqa: E402


class FakeRPCServer:
    def __init__(self):
        self.seen = []
        self.responses = {}
        self.status = 200
        srv = self
        cookie_file = Path(tempfile.mkdtemp()) / ".cookie"
        cookie_file.write_text("__cookie__:s3cret")
        self.cookie_file = cookie_file

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                srv.seen.append((self.headers.get("Authorization"), body))
                resp = srv.responses.get(body["method"], {"result": None})
                if isinstance(resp, str):
                    # A raw body, for amounts a float could not carry.
                    out = resp.replace("__ID__", str(body["id"])).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                if isinstance(resp, Exception):
                    self.send_response(500)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html>boom</html>")
                    return
                out = json.dumps(dict(resp, id=body["id"])).encode()
                self.send_response(srv.status if "error" not in resp else 500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.http = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()


def test_credentials_and_calls(t):
    f = FakeRPCServer()
    try:
        n = R.NodeRPC(url=f.url, user="u", password="p")
        f.responses["getblockchaininfo"] = {"result": {"blocks": 7, "chain": "test", "mediantime": 99}}
        t.eq(n.chain_height(), 7, "a result comes back")
        t.eq(n.chain_name(), "test", "the chain name")
        t.eq(n.median_time(), 99, "the median time")
        auth, body = f.seen[0]
        t.eq(auth, "Basic dTpw", "user/password become Basic auth")
        t.eq(body["method"], "getblockchaininfo", "the method is sent")
        t.eq(body["jsonrpc"], "1.0", "as JSON-RPC 1.0")
        c = R.NodeRPC(url=f.url, cookie=str(f.cookie_file))
        c.chain_height()
        t.eq(f.seen[-1][0], "Basic X19jb29raWVfXzpzM2NyZXQ=", "a cookie file is read")
        f.cookie_file.write_text("__cookie__:rotated")
        c.call("getblockchaininfo")
        t.eq(f.seen[-1][0], "Basic X19jb29raWVfXzpyb3RhdGVk", "and re-read on every call")
        # The tip is held for a moment: drawing a page asks for the height, the
        # clock and the node's reachability, and that is one round trip rather
        # than three.
        c.forget_chain_info()
        before = len(f.seen)
        c.chain_height(); c.chain_name(); c.median_time()
        t.eq(len(f.seen), before + 1, "the tip is asked for once, not once per question")
        c.forget_chain_info()
        c.chain_height()
        t.eq(len(f.seen), before + 2, "and asked again when told to forget it")
        try:
            R.NodeRPC(url=f.url, cookie=str(f.cookie_file) + ".missing")
            t.ok(False, "a missing cookie file is refused at startup")
        except R.RPCError as e:
            t.ok("does not exist" in str(e), "a missing cookie file is refused at startup")
    finally:
        f.close()


def test_errors_surface_the_nodes_message(t):
    f = FakeRPCServer()
    try:
        n = R.NodeRPC(url=f.url, user="u", password="p")
        f.responses["getstakerinfo"] = {"error": {"code": -8, "message": "Too many parameters"}, "result": None}
        try:
            n.call("getstakerinfo", True, True)
            t.ok(False, "an RPC error raises")
        except R.RPCError as e:
            t.ok("Too many parameters" in str(e), "with the node's own message")
        f.responses["getblockhash"] = RuntimeError("html")
        try:
            n.call("getblockhash", 0)
            t.ok(False, "a non-JSON error raises")
        except R.RPCError as e:
            t.ok("HTTP 500" in str(e), "naming the status")
        dead = R.NodeRPC(url="http://127.0.0.1:1", user="u", password="p", timeout=1)
        try:
            dead.chain_height()
            t.ok(False, "a refused connection raises")
        except R.RPCError:
            t.ok(True, "a refused connection raises RPCError")
    finally:
        f.close()


def test_amounts_are_exact(t):
    f = FakeRPCServer()
    try:
        n = R.NodeRPC(url=f.url, user="u", password="p")
        f.responses["gettxout"] = '{"result": {"value": 999999999.99999999, "asset": "%s"}, "error": null, "id": __ID__}' % ("aa" * 32)
        out = n.txout("ab" * 32, 0)
        t.ok(isinstance(out["value"], Decimal), "amounts are Decimals, not floats")
        t.eq(R.to_atoms(out["value"]), 99999999999999999, "and convert to atoms exactly")
        t.eq(R.to_atoms("0.00000001"), 1, "one atom")
        t.eq(R.to_atoms(5), 5, "an int is already atoms")
        f.responses["getmempoolinfo"] = {"result": {"minrelaytxfee": 0.00001}}
        t.eq(n.min_relay_fee_atoms_per_kvb(), 1000, "the relay floor in atoms per kvB")
    finally:
        f.close()


def test_stake_views_fall_back_in_order(t):
    f = FakeRPCServer()
    try:
        n = R.NodeRPC(url=f.url, user="u", password="p")
        f.responses["getstakerinfo"] = {"result": {"02" + "aa" * 32: {"weight": 5, "delegated": True, "signer": "03" + "bb" * 32}}}
        w, by = n.controller_weights()
        t.eq(by, True, "a node that answers by controller")
        t.eq(w["02" + "aa" * 32]["signer"], "03" + "bb" * 32, "reports the pool")
        state = {"n": 0}

        class Flip(R.NodeRPC):
            def call(self, method, *params):
                if params:
                    raise R.RPCError("getstakerinfo: Too many parameters")
                return {"02" + "aa" * 32: 5}

        w, by = Flip(url=f.url, user="u", password="p").controller_weights()
        t.eq(by, False, "an older node falls back to the signer view and says so")
        t.eq(w["02" + "aa" * 32]["weight_atoms"], 5, "with the weights it has")

        class NoPos(R.NodeRPC):
            def call(self, method, *params):
                raise R.RPCError("getstakerinfo: Proof-of-Stake (con_pos) is not enabled on this chain")

        w, by = NoPos(url=f.url, user="u", password="p").controller_weights()
        t.eq((w, by), ({}, None), "a chain without staking has nobody to report, and is not an outage")
    finally:
        f.close()
