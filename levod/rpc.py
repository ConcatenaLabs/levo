"""A small JSON-RPC client for a Sequentia node.

Levo reads the chain and never writes to it. The node is asked three kinds of
question -- who is staking and how much (`getstakerinfo`), is this sale UTXO
really funded and still unspent (`gettxout`, `scantxoutset`), and where is the
tip (`getblockchaininfo`) -- and nothing else. There is no wallet call here on
purpose: levod holds no keys and can move no funds, so a compromised levod can
misinform users but cannot rob them.
"""

import json
import os
import urllib.request
import urllib.error
from base64 import b64encode
from pathlib import Path


class RPCError(RuntimeError):
    pass


class NodeRPC:
    def __init__(self, url=None, user=None, password=None, cookie=None, timeout=20):
        self.url = url or os.environ.get("LEVOD_RPC_URL", "http://127.0.0.1:7041")
        self.timeout = timeout
        user = user or os.environ.get("LEVOD_RPC_USER")
        password = password or os.environ.get("LEVOD_RPC_PASSWORD")
        if not user:
            cookie = cookie or os.environ.get("LEVOD_RPC_COOKIE")
            if cookie and Path(cookie).is_file():
                user, password = Path(cookie).read_text().strip().split(":", 1)
        self._auth = None
        if user:
            self._auth = b64encode(("%s:%s" % (user, password or "")).encode()).decode()
        self._id = 0

    def call(self, method, *params):
        self._id += 1
        body = json.dumps({"jsonrpc": "1.0", "id": self._id,
                           "method": method, "params": list(params)}).encode()
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json"})
        if self._auth:
            req.add_header("Authorization", "Basic " + self._auth)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as e:
            # The node reports RPC-level failures with a non-2xx status and a
            # JSON body; surface its message rather than a bare 500.
            try:
                payload = json.loads(e.read())
            except Exception:
                raise RPCError("%s: HTTP %s" % (method, e.code))
        except Exception as e:
            raise RPCError("%s: %s" % (method, e))
        if payload.get("error"):
            raise RPCError("%s: %s" % (method, payload["error"].get("message", payload["error"])))
        return payload.get("result")

    # --- the questions Levo actually asks ---------------------------------

    def staker_weights(self):
        """{staker public key hex: stake weight in policy-asset atoms}.

        Weights keyed by SIGNER: what the chain uses to produce blocks. A
        delegated stake appears here under the pool operator, not its owner.
        """
        return {str(k).lower(): int(v) for k, v in (self.call("getstakerinfo") or {}).items()}

    def controller_weights(self):
        """{controller public key hex: {weight_atoms, delegated, signer}}.

        Weight keyed by the key that OWNS the stake, which is the question Levo
        is actually asking. A stake delegated to a pool still belongs to the
        controller -- delegation lends block-signing rights and never the coins,
        and only the controller can ever spend them -- so it counts for the
        controller here. The pool operator is credited with its own stake only,
        rather than with weight its depositors put up.

        Falls back to signer-keyed weights on a node that does not support the
        question, and says so, because silently reporting a delegator as having
        no stake would lock them out with no explanation.
        """
        try:
            raw = self.call("getstakerinfo", True, True) or {}
            out = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    out[str(k).lower()] = {
                        "weight_atoms": int(v.get("weight", 0)),
                        "delegated": bool(v.get("delegated")),
                        "signer": (v.get("signer") or "").lower() or None,
                    }
                else:
                    out[str(k).lower()] = {"weight_atoms": int(v),
                                           "delegated": False, "signer": None}
            return out, True
        except RPCError:
            return ({k: {"weight_atoms": v, "delegated": False, "signer": None}
                     for k, v in self.staker_weights().items()}, False)

    def delegations(self):
        """{controller: signer} for every live delegation, or {} if unavailable."""
        try:
            return {str(k).lower(): str(v).lower()
                    for k, v in (self.call("getdelegationinfo") or {}).items()}
        except RPCError:
            return {}

    def chain_height(self):
        return int(self.call("getblockchaininfo")["blocks"])

    def txout(self, txid, vout, include_mempool=True):
        """The unspent output, or None if it does not exist or has been spent."""
        return self.call("gettxout", txid, int(vout), bool(include_mempool))
