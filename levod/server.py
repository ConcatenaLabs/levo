#!/usr/bin/env python3
"""levod -- the Levo backend.

It serves the built single-page app and the API from one origin, so a single
reverse-proxy route covers the whole platform.

What this process can and cannot do is worth being precise about, because it
determines how much trust a user has to place in whoever operates it.

It CANNOT move funds. levod holds no private keys and signs nothing. The
transactions it builds are unsigned, and only the buyer's (or the project's)
wallet can complete them. Sale tokens sit in a covenant that only the sell and
reclaim leaves can spend, and neither leaf will accept a signature from Levo,
because neither leaf mentions Levo at all.

It CAN mislead. A hostile or broken levod could show a sale that is not really
funded, quote a price that is not really the covenant's, or hide a listing. That
is why every response that matters carries the terms needed to check it locally:
a client that rebuilds the sale address from the published terms and compares it
to the funded output does not have to trust this server about the thing that
matters most.
"""

import json
import os
import signal
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

import address as ADDR    # noqa: E402
import auth as A          # noqa: E402
import market as M        # noqa: E402
import rails as R         # noqa: E402
import rpc as RPC         # noqa: E402
import sale as S          # noqa: E402
import store as ST        # noqa: E402
import tiers as T         # noqa: E402
import watcher as W       # noqa: E402

MAX_BODY = 256 * 1024
WATCHER_RPC_TIMEOUT = 300         # a UTXO-set scan on a big chain takes a while
# The chains whose tokens are worth something. Everything else is a test
# chain, including one this build has never heard of.
MAINNET_CHAINS = ("sequentia", "main")

BUSY_WAIT_SECONDS = 5            # how long a request waits for a free handler
MAX_HANDLERS = 64                 # concurrent requests before the rest wait
# Polls that must fail in a row before health calls the watcher broken. One is
# a bad minute on the node; three in a row is something to look at.
WATCHER_FAILURES_BEFORE_UNHEALTHY = 3


class RateLimit:
    """A token bucket per client, for the endpoints anybody can hit.

    Issuing a challenge and checking a signature both cost the server real
    work and cost the caller nothing, so without a ceiling one client can
    evict every pending login or pin the interpreter. Clients are told apart
    by the address the reverse proxy forwards, or by the peer when there is
    no proxy.
    """

    def __init__(self, per_minute, burst=None):
        self.rate = per_minute / 60.0
        self.burst = burst or per_minute
        self._buckets = {}
        self._lock = threading.Lock()

    def allow(self, client, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            tokens, at = self._buckets.get(client, (self.burst, now))
            tokens = min(self.burst, tokens + (now - at) * self.rate)
            if tokens < 1:
                self._buckets[client] = (tokens, now)
                return False
            self._buckets[client] = (tokens - 1, now)
            if len(self._buckets) > 50_000:
                # Forget the quietest clients rather than grow without bound.
                for k, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[:10_000]:
                    del self._buckets[k]
            return True


class App:
    """Everything the request handlers need, assembled once.

    `node` is injectable so the demo and the test drill can supply a stub. It is
    taken once and shared by every part that needs the chain, so there is no way
    to end up with half the process talking to one node and half to another.
    """

    def __init__(self, node=None):
        self.node = node or RPC.NodeRPC()
        self.links = T.StakeLinks()
        self.policy = T.TierPolicy(T.tiers_from_env())
        self.reader = T.StakeReader(self.node, self.links, self.policy)
        self.store = ST.Store()
        payment_asset = os.environ.get("LEVOD_PAYMENT_ASSET", M.DEFAULT_PAYMENT_ASSET).lower()
        payment_label = os.environ.get("LEVOD_PAYMENT_LABEL", "USDX")
        # Every amount of the payment asset is displayed and typed through
        # this. Eight is what Elements issues by default and what the testnet's
        # USDX uses; an asset with another precision would be quoted a hundred
        # times wrong in both directions.
        try:
            payment_decimals = int(os.environ.get("LEVOD_PAYMENT_DECIMALS", "8"))
        except ValueError:
            payment_decimals = 8
            _log_warning("LEVOD_PAYMENT_DECIMALS is not a number; using 8")
        if not 0 <= payment_decimals <= 8:
            payment_decimals = 8
            _log_warning("LEVOD_PAYMENT_DECIMALS is out of range; using 8")
        self.payment_decimals = payment_decimals
        self._chain = None
        self._stake_label_env = os.environ.get("LEVOD_STAKE_LABEL")
        # levod is often started beside the node it reads, and wins the race.
        # Asking once would then label the testnet's stake SEQ and report no
        # chain at all for the life of the process, so it is asked again until
        # it answers.
        self.stake_label = self._stake_label_env or "SEQ"
        self.chain          # ask now; the property asks again until it answers
        # Addresses are encoded and decoded against this prefix, so getting it
        # wrong sends tokens nowhere. The chain the node reports decides it
        # unless an operator says otherwise.
        self._hrp_env = os.environ.get("LEVOD_HRP")
        self._warned_hrp = False
        self.explorer_url = os.environ.get("LEVOD_EXPLORER_URL", "").rstrip("/")
        self.site_links = _site_links(os.environ.get("LEVOD_LINKS"))
        # Where this Levo's own source is. A visitor is told to run a command
        # and to rebuild a sale address themselves; both need somewhere to get
        # the code, and an operator running a fork should point at their own.
        self.source_url = (os.environ.get("LEVOD_SOURCE_URL")
                           or "https://github.com/ConcatenaLabs/levo").rstrip("/")
        self.rails = R.Rails(payment_asset, payment_label,
                             R.NodeRateSource(
                                 self.node,
                                 btc_label=os.environ.get("LEVOD_BTC_RATE_LABEL")
                                 or R.BTC_RATE_LABEL))
        self.watcher = None
        self.operators = _accounts(os.environ.get("LEVOD_OPERATORS"))
        self.market = M.Platform(self.store, self.reader, self.rails, self.node,
                                 hrp=lambda: self.hrp, payment_asset=payment_asset,
                                 payment_label=payment_label,
                                 stake_label=self.stake_label,
                                 payment_decimals=self.payment_decimals,
                                 operators=self.operators,
                                 on_stale=lambda: self.watcher and self.watcher.nudge())
        watch_node = self.node.with_timeout(WATCHER_RPC_TIMEOUT) \
            if hasattr(self.node, "with_timeout") else self.node
        self.watcher = W.Watcher(
            self.market, watch_node,
            interval=int(os.environ.get("LEVOD_WATCH_SECONDS", "60")),
            hrp=lambda: self.hrp, log=_log_error, note=_log_notice)
        self.challenges = A.Challenges(site="Levo")
        self.stake_challenges = A.Challenges(site="Levo")
        self.sessions = A.Sessions()
        self.auth_limit = RateLimit(per_minute=int(os.environ.get("LEVOD_AUTH_PER_MINUTE", "30")))
        self.write_limit = RateLimit(per_minute=int(os.environ.get("LEVOD_WRITES_PER_MINUTE", "120")))
        # Reads are cheap per request and not free: the board asks the node for
        # its tip, and the fee route asks for the mempool and the rate table.
        # One caller in a loop could fill every handler with requests blocked
        # on the node while real visitors wait behind them.
        self.read_limit = RateLimit(per_minute=int(os.environ.get("LEVOD_READS_PER_MINUTE", "600")))
        self.handlers = threading.BoundedSemaphore(MAX_HANDLERS)
        # The reverse proxy in front of levod, whose X-Forwarded-For is worth
        # believing. Loopback by default, because that is where a proxy on the
        # same host connects from; set it empty when levod is exposed directly.
        # The address this Levo is reached at, named in the statement a wallet
        # is asked to sign. Configured where there is a proxy in front, since
        # the Host header is then whatever the proxy passes on.
        self.origin = (os.environ.get("LEVOD_ORIGIN") or "").strip().rstrip("/") or None
        self.trusted_proxies = {a for a in re.split(
            r"[,\s]+", os.environ.get("LEVOD_TRUSTED_PROXIES", "127.0.0.1 ::1")) if a}
        self.webroot = Path(os.environ.get(
            "LEVOD_WEBROOT",
            str(Path(__file__).resolve().parent.parent / "web" / "dist")))
        # Whether a built app was there when levod started. If it was and it
        # is not now, something removed it and every page is a 404.
        self.had_webroot = (self.webroot / "index.html").is_file()
        self.verbose = bool(os.environ.get("LEVOD_VERBOSE"))

    @property
    def hrp(self):
        """The chain's address prefix. Derived from the chain the node reports,
        which is asked for until it answers: a levod started while its node was
        down would otherwise encode every address with the wrong prefix for the
        life of the process, and a later restart would silently fix it."""
        known = ADDR.hrp_for(self.chain, default=None)
        if self._hrp_env:
            return self._hrp_env
        if known:
            return known
        # A chain this build has never heard of. Guessing would encode every
        # address with the wrong prefix, so it says so once and uses the
        # testnet prefix, which is what a new chain is nine times in ten.
        if not self._warned_hrp:
            self._warned_hrp = True
            _log_warning("the node reports the chain %r, which has no address "
                         "prefix here; using tb. Set LEVOD_HRP."
                         % (self.chain or "unknown"))
        return "tb"

    @property
    def chain(self):
        """The chain the node reports, or what the environment was told to
        assume. Cached once it is known, since a node does not change chains
        under a running process."""
        if self._chain:
            return self._chain
        try:
            name = self.node.chain_name()
        except Exception:
            name = None
        if not name:
            return os.environ.get("LEVOD_CHAIN", "")
        self._chain = name
        if not self._stake_label_env:
            self.stake_label = "tSEQ" if name == "test" else "SEQ"
            if getattr(self, "market", None) is not None:
                self.market.stake_label = self.stake_label
        return name

    def config(self):
        """The facts a client needs to label and link things correctly."""
        tiers = self.policy.tiers
        first = tiers[1] if len(tiers) > 1 else tiers[0]
        return {
            "chain": self.chain,
            "hrp": self.hrp,
            # Every chain but the one mainnet. A deployment on a chain this
            # build does not know is a test chain until it says otherwise,
            # because telling a visitor that play money is real is the worse
            # mistake of the two.
            "testnet": self.chain not in MAINNET_CHAINS,
            "explorer_url": self.explorer_url,
            "payment": {"asset": self.rails.payment_asset,
                        "label": self.rails.payment_label,
                        "decimals": self.payment_decimals},
            "stake": {"label": self.stake_label, "decimals": 8},
            "staking_floor_atoms": T.POS_MIN_STAKE_ATOMS,
            "first_tier_atoms": first.min_stake_atoms,
            "first_tier_is_chain_floor": first.min_stake_atoms == T.POS_MIN_STAKE_ATOMS,
            "links": self.site_links,
            "source_url": self.source_url,
        }

    def tiers_note(self):
        first = self.policy.tiers[1] if len(self.policy.tiers) > 1 else None
        if first is None:
            return "Only staked Sequence counts, and only for keys you have proven you control."
        text = "The first tier begins at %s staked." % _seq(first.min_stake_atoms, self.stake_label)
        if first.min_stake_atoms == T.POS_MIN_STAKE_ATOMS:
            text += (" That is the chain's own blocksigner floor: below it, "
                     "consensus ignores a staker's weight entirely.")
        else:
            text += (" The chain's own blocksigner floor is %s."
                     % _seq(T.POS_MIN_STAKE_ATOMS, self.stake_label))
        return text + (" Only staked Sequence counts, and only for keys you have "
                       "proven you control.")


class Handler(BaseHTTPRequestHandler):
    server_version = "levod"
    sys_version = ""
    timeout = 30                       # a client that stops sending frees its thread
    app = None

    def version_string(self):
        return "levod"                 # no interpreter version on the wire

    def origin(self):
        """Where the caller reached this Levo: what the operator configured, or
        else the host the request names. It goes into the statement a wallet
        shows before signing, so it has to be the address the person is
        actually looking at."""
        if self.app and self.app.origin:
            return self.app.origin
        host = (self.headers.get("Host") or "").strip()[:100]
        if not host or any(c in host for c in " \r\n"):
            return None
        proto = "http"
        peer = self.client_address[0] if self.client_address else "?"
        if self.app and peer in self.app.trusted_proxies:
            proto = (self.headers.get("X-Forwarded-Proto") or "http").strip().lower()
            if proto not in ("http", "https"):
                proto = "http"
        return "%s://%s" % (proto, host)

    def client(self):
        """Who is asking: the address the proxy forwards when the peer is a
        proxy Levo was told to believe, otherwise the peer itself.

        The header is a claim, and anything that can reach levod can make it.
        Believing it from an address that is not the reverse proxy would let a
        caller pick their own rate-limit bucket per request, which is the same
        as having no rate limit at all.
        """
        peer = self.client_address[0] if self.client_address else "?"
        if self.app and peer in self.app.trusted_proxies:
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()[:64]
        return peer

    def handle_one_request(self):
        # Wait for a byte before taking a slot.
        #
        # A browser opens several connections and keeps them open; taking a
        # slot the moment one is accepted meant a handful of idle tabs could
        # hold every slot levod has, and real requests -- an uptime check
        # included -- waited behind connections that had sent nothing. Peeking
        # first costs one blocking read on a socket that already has its own
        # timeout, and after it the slot is held only while a request is
        # actually being served.
        try:
            self.rfile.peek(1) if hasattr(self.rfile, "peek") else None
        except Exception:
            self.close_connection = True
            return
        # The slot is taken before the request line is PARSED, so nothing about
        # the request is known here yet. BaseHTTPRequestHandler's reply helpers
        # all need a parsed request, so the refusal is written as bytes and the
        # connection closed.
        acquired = self.app.handlers.acquire(timeout=BUSY_WAIT_SECONDS) if self.app else True
        if not acquired:
            body = b'{"error": "levod is busy; try again in a moment"}'
            try:
                self.wfile.write(b"HTTP/1.1 503 Service Unavailable\r\n"
                                 b"Content-Type: application/json\r\n"
                                 b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                                 b"Retry-After: 2\r\n"
                                 b"Connection: close\r\n\r\n" + body)
            except Exception:
                pass
            self.close_connection = True
            return
        try:
            super().handle_one_request()
        finally:
            if self.app:
                self.app.handlers.release()

    # --- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):
        if not self.app or not self.app.verbose:
            # Health checks, tier tables and asset fetches say nothing when
            # they succeed; the journal is for what an operator acts on.
            path = urlparse(self.path).path
            code = str(args[1]) if len(args) > 1 else ""
            quiet = path in ("/api/health", "/api/tiers", "/api/rails", "/api/config") \
                or path.startswith("/assets/") or path.endswith((".svg", ".png", ".woff2"))
            if quiet and code.startswith("2"):
                return
        sys.stderr.write("levod %s %s\n" % (self.client(), fmt % args))

    def log_error(self, fmt, *args):
        pass                            # the request line is logged once, above

    def _send(self, code, body, ctype, cache="no-store", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; "
                             "style-src 'self' 'unsafe-inline'; font-src 'self'; "
                             "connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass                    # the client left; nothing to report

    def _json(self, code, payload, cache="no-store", headers=None):
        body = json.dumps(payload, indent=2, sort_keys=True).encode()
        self._send(code, body, "application/json", cache=cache, headers=headers)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise Unsupported(411, "send the request body with a Content-Length")
        raw = self.headers.get("Content-Length") or "0"
        try:
            n = int(raw)
        except ValueError:
            raise ValueError("Content-Length must be a number")
        if n < 0:
            raise ValueError("Content-Length must be a number, 0 or more")
        if n > MAX_BODY:
            raise ValueError("the request body is larger than %d bytes" % MAX_BODY)
        if not n:
            return {}
        data = self.rfile.read(n)
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("the request body is not valid JSON")
        except RecursionError:
            # A body nested a few thousand deep exhausts the parser's stack.
            # That is a malformed request, not a fault in levod, and answering
            # it with a traceback per attempt is a log flood anyone can start.
            raise ValueError("the request body is nested too deeply")
        if not isinstance(body, dict):
            raise ValueError("the request body must be a JSON object")
        return body

    def _account(self):
        """The logged-in account, or None."""
        h = self.headers.get("Authorization") or ""
        token = h[7:].strip() if h.lower().startswith("bearer ") else None
        return self.app.sessions.verify(token) if token else None

    def _require_account(self):
        acct = self._account()
        if not acct:
            raise Unauthorised("sign in with your wallet first")
        return acct

    # --- routing ------------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        """What this path takes -- this one, not every path.

        Answering the same list everywhere told a client that /api/health takes
        DELETE and that a static file takes POST.
        """
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        allowed = _methods_for(parts[1:]) if parts[:1] == ["api"] else None
        if allowed is None:
            allowed = ["GET"] if not path.startswith("/api/") else []
        self.send_response(204)
        self.send_header("Allow", ", ".join(allowed + ["HEAD", "OPTIONS"])
                         if allowed else "OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        self._method_not_allowed()

    def _method_not_allowed(self):
        body = json.dumps({"error": "method not allowed"}).encode()
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self, method):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        try:
            if path == "/api" or path.startswith("/api/"):
                return self._api(method, path, query)
            if method != "GET":
                return self._method_not_allowed()
            return self._static(path)
        except Unsupported as e:
            # A 429 without a Retry-After tells a client to back off and gives
            # it nothing to back off by.
            self._json(e.code, {"error": str(e)},
                       headers={"Retry-After": "60"} if e.code == 429 else None)
        except Unauthorised as e:
            self._json(401, {"error": str(e)})
        except M.NotFound as e:
            self._json(404, {"error": str(e)})
        except M.NotAuthorised as e:
            self._json(403, {"error": str(e)})
        except S.CapExceeded as e:
            self._json(409, {"error": str(e), "allowance_atoms": e.allowance_atoms,
                             "enforced_by": "levo"})
        except (M.PlatformError, S.SaleError, A.BadSignature, R.RailUnavailable,
                ValueError) as e:
            self._json(400, {"error": str(e)})
        except KeyError as e:
            # A field the request should have carried and did not. The caller
            # is at fault and deserves to hear which field.
            #
            # AttributeError is deliberately NOT caught here: it is what a bug
            # in levod looks like, and answering one as "malformed request"
            # blamed the caller for the server's mistake and logged nothing at
            # all. TypeError stays, because body parsing still leans on it.
            self._json(400, {"error": "malformed request: %s" % _describe(e)})
        except TypeError as e:
            _log_error("malformed request on %s %s: %s" % (method, path, e))
            _log_error(traceback.format_exc())
            self._json(400, {"error": "malformed request"})
        except RPC.RPCError as e:
            self._json(502, {"error": "the Sequentia node is unreachable or "
                                      "refused the query: %s" % e})
        except Exception:
            _log_error("unhandled error on %s %s" % (method, path))
            _log_error(traceback.format_exc())
            self._json(500, {"error": "internal error"})

    def _api(self, method, path, query):
        app = self.app
        parts = [p for p in path.split("/") if p][1:]      # drop "api"

        # -- health and static facts ----------------------------------------
        if method == "GET" and parts == ["health"]:
            try:
                h = app.node.chain_height()
                node = {"reachable": True, "height": h, "chain": app.chain}
            except Exception as e:
                node = {"reachable": False, "error": str(e), "chain": app.chain}
            w = app.watcher
            age = (time.time() - w.last_run) if w.last_run else None
            stale = bool(w._thread) and (age is None or age > 3 * w.interval)
            # A watcher that runs on time but fails every poll reconciles
            # nothing, and a sale that has sold out goes on showing as open. A
            # monitor has to see that. One poll erroring is not that: a single
            # RPC timeout on one sale is a normal minute on a busy node, and a
            # health check that flips to 503 for it teaches its reader to
            # ignore it.
            failing = w.consecutive_errors >= WATCHER_FAILURES_BEFORE_UNHEALTHY
            # A levod that cannot write its state file is serving a ledger that
            # exists only in its own memory: the next restart reverts to the
            # last write that worked, and every purchase recorded since is
            # gone. Nothing about that is visible from the outside otherwise.
            write_error = app.store.write_error
            # levod serves the app and the API from one origin, so an emptied
            # or missing web/dist is a site that answers 404 for every page
            # while every API check passes. An uptime check watching this
            # endpoint has to see that. Only when a bundle was there at
            # startup: an API-only run is not broken.
            serving = (not app.had_webroot) or (app.webroot / "index.html").is_file()
            ok = (node["reachable"] and not stale and not failing
                  and not write_error and serving)
            return self._json(200 if ok else 503, {
                "service": "levod", "ok": ok, "node": node,
                "app": {"serving": serving, "webroot": str(app.webroot)},
                "state_file": {"writable": not write_error,
                               "unsaved_changes": bool(app.store.dirty),
                               "last_error": write_error},
                "watcher": {"running": bool(w._thread and w._thread.is_alive()),
                            "last_run_age_seconds": int(age) if age is not None else None,
                            "reconciling": not failing,
                            "consecutive_errors": w.consecutive_errors,
                            "unverified_sales": list(w.unverified),
                            "last_error": w.last_error},
            })

        if method == "GET" and parts == ["config"]:
            # Labels, prefixes and links: the same answer for every visitor,
            # and the first thing every page asks for.
            return self._json(200, app.config(), cache="public, max-age=30")

        if method == "GET" and parts == ["tiers"]:
            return self._json(200, cache="public, max-age=30", payload={
                "tiers": app.policy.to_json(),
                "staking_floor_atoms": T.POS_MIN_STAKE_ATOMS,
                "stake_label": app.stake_label,
                "note": app.tiers_note(),
                "caps_enforced_by": "levo",
            })

        if method == "GET" and parts == ["rails"]:
            return self._json(200, {"rails": app.rails.available()})

        if method == "GET" and parts == ["watcher"]:
            w = app.watcher
            return self._json(200, {
                "running": bool(w._thread and w._thread.is_alive()),
                "interval_seconds": w.interval,
                "last_run": w.last_run,
                "last_error": w.last_error,
                "unverified_sales": list(w.unverified),
                "what_it_does": "reads every sale from the chain -- the UTXO "
                                "set and the mempool -- so a purchase made "
                                "without Levo still moves the sale, and a "
                                "funding the chain no longer has stops the sale "
                                "being investable. It ends a sale only on "
                                "evidence, never on silence.",
                "unverified_means": "a sale whose funding this levod cannot "
                                    "place in the chain, which happens to state "
                                    "restored from a backup. Such a sale is left "
                                    "exactly as it was rather than guessed at.",
            })

        if method in ("GET", "HEAD") and parts[:1] != ["health"] and \
                not app.read_limit.allow(self.client()):
            # Health is left out on purpose: an uptime check must never be the
            # thing that trips the limit.
            raise Unsupported(429, "too many requests from this address; slow down")

        # -- auth -------------------------------------------------------------
        if method == "POST" and parts[:1] in (["auth"], ["stake"]) and \
                not app.auth_limit.allow(self.client()):
            raise Unsupported(429, "too many sign-in attempts from this address; "
                                   "wait a minute and try again")
        if method in ("POST", "PATCH", "DELETE") and parts[:1] == ["projects"] and \
                not app.write_limit.allow(self.client()):
            raise Unsupported(429, "too many requests from this address; wait a "
                                   "minute and try again")

        if method == "POST" and parts == ["auth", "challenge"]:
            return self._json(200, app.challenges.issue("Sign in to Levo",
                                                        origin=self.origin()))

        if method == "POST" and parts == ["auth", "verify"]:
            b = self._body()
            message = _str(b, "message")
            app.challenges.redeem(message)          # burns the nonce first
            pubkey = A.recover_pubkey(message, _str(b, "signature"))
            address = b.get("address")
            if address:
                # The caller names the address it signed with, so a signature
                # over slightly different bytes is a clear error rather than a
                # phantom account nobody controls.
                if not A.key_matches_address(pubkey, str(address), hrp=app.hrp):
                    raise A.BadSignature(
                        "that signature was not made by the key behind %s. The "
                        "text that was signed differs from the challenge, or the "
                        "wallet signed with another key" % address)
            return self._json(200, {
                "account": pubkey,
                "token": app.sessions.issue(pubkey),
                "note": "your account is the public key that signed; Levo stores "
                        "no password and can produce no signature for you",
            })

        if method == "GET" and parts == ["me"]:
            acct = self._require_account()
            standing = app.reader.standing(acct)
            if app.links.dirty:
                with app.market.lock:
                    app.links.dirty = False
                    app.market.save()
            # Whether this account may flag a listing on this Levo. Without it
            # a client cannot know whether to offer the control at all.
            standing["operator"] = acct.lower() in app.market.operators
            return self._json(200, standing)

        if method == "GET" and parts == ["me", "projects"]:
            acct = self._require_account()
            return self._json(200, app.market.projects_of(
                acct, limit=_int_param(query, "limit"),
                offset=_int_param(query, "offset") or 0))

        if method == "GET" and parts == ["me", "positions"]:
            acct = self._require_account()
            standing = app.reader.standing(acct)
            tier = app.policy.for_stake(standing["stake_atoms"])
            page = app.market.positions(acct, tier,
                                        limit=_int_param(query, "limit"),
                                        offset=_int_param(query, "offset") or 0)
            page["tier"] = tier.to_json()
            return self._json(200, page)

        # -- linking a staking key --------------------------------------------
        if method == "POST" and parts == ["stake", "challenge"]:
            acct = self._require_account()
            b = self._body()
            staker = _str(b, "staker_pubkey").lower()
            _check_pubkey(staker)
            ch = app.stake_challenges.issue(
                purpose=T.StakeLinks.PURPOSE,
                extra_lines=T.StakeLinks.binding_lines(acct, staker),
                origin=self.origin())
            return self._json(200, ch)

        if method == "POST" and parts == ["stake", "link"]:
            acct = self._require_account()
            b = self._body()
            message = _str(b, "message")
            staker = _str(b, "staker_pubkey").lower()
            _check_pubkey(staker)
            app.stake_challenges.redeem(message)
            # The statement names both parties, so a signature proving control
            # of this key cannot be replayed onto a different account. Check the
            # NAMED FIELDS as well as the text: they are the contract.
            named = _fields_of(message)
            if named.get("Account") != acct:
                raise ValueError("the signed statement names account %r, but you "
                                 "are signed in as %r"
                                 % (named.get("Account"), acct))
            if named.get("Staking key") != staker:
                raise ValueError("the signed statement names staking key %r, "
                                 "not %r" % (named.get("Staking key"), staker))
            if not A.verify_signature(message, _str(b, "signature"), staker):
                raise A.BadSignature("that signature was not made by %s; the wallet "
                                     "signed with a different key" % staker)
            with app.market.lock:
                app.links.link(acct, staker)
                app.market.save()
            return self._json(200, app.reader.standing(acct))

        if method == "POST" and parts == ["stake", "unlink"]:
            acct = self._require_account()
            b = self._body()
            staker = _str(b, "staker_pubkey").lower()
            _check_pubkey(staker)
            if staker not in app.links.keys_for(acct):
                raise M.NotFound("that staking key is not linked to your account")
            with app.market.lock:
                app.links.unlink(acct, staker)
                app.market.save()
            return self._json(200, app.reader.standing(acct))

        if method == "POST" and parts == ["outputs", "check"]:
            # Which of these outputs a covenant purchase could spend. The
            # answer is the node's, not a guess from an address form.
            self._require_account()
            b = self._body()
            return self._json(200, {"outputs": app.market.describe_inputs(
                b.get("outputs") or b.get("inputs") or [])})

        # -- projects ----------------------------------------------------------
        if method == "GET" and parts == ["projects"]:
            page = app.market.public_projects(
                status=_one(query, "status"), q=_one(query, "q"),
                sort=_one(query, "sort") or "new",
                limit=_int_param(query, "limit"),
                offset=_int_param(query, "offset") or 0)
            page["node_reachable"] = app.market.height() is not None
            return self._json(200, page)

        if method == "GET" and len(parts) == 3 and parts[0] == "projects" \
                and parts[2] == "purchases":
            # Levo's own ledger for one sale: what it recorded, and what each
            # account has committed. The issuer and an operator can read it;
            # nobody else, because it names accounts and amounts.
            acct = self._require_account()
            return self._json(200, app.market.sale_ledger(
                acct, parts[1], limit=_int_param(query, "limit"),
                offset=_int_param(query, "offset") or 0))

        if method == "GET" and len(parts) == 3 and parts[0] == "projects" \
                and parts[2] == "fee":
            # What a fee should be for a transaction against this sale, before
            # there is a transaction to measure. The buy path gets this with
            # its quote; a reclaim has no quote, and guessing a figure in a
            # form is how a transaction ends up below the relay floor.
            p = app.market.project(parts[1])
            if p.sale is None:
                raise M.NotFound("this project has no sale")
            kind = _one(query, "kind") or "buy"
            if kind not in ("buy", "reclaim"):
                raise M.PlatformError("kind is buy or reclaim")
            n = _int_param(query, "inputs")
            return self._json(200, app.market.fee_advice(
                p.sale, n_inputs=max(1, min(int(n or 1), M.MAX_INPUTS)),
                fee_asset=_one(query, "asset"), kind=kind))

        if len(parts) == 2 and parts[0] == "projects":
            slug = parts[1]
            if method == "GET":
                d = app.market.project_detail(slug)
                d["node_reachable"] = app.market.height() is not None
                return self._json(200, d)
            if method == "PATCH":
                acct = self._require_account()
                p = app.market.update_project(acct, slug, self._body())
                return self._json(200, app.market.project_detail(p.slug))
            if method == "DELETE":
                acct = self._require_account()
                app.market.withdraw(acct, slug)
                return self._json(200, {"withdrawn": slug})

        if method == "POST" and parts == ["projects"]:
            acct = self._require_account()
            b = self._body()
            p = app.market.list_project(acct, b.get("project") or {},
                                        b.get("terms") or {})
            return self._json(201, {"project": p.to_json(),
                                    "lock": app.market.lock_instructions(p)})

        if method == "POST" and len(parts) == 3 and parts[0] == "projects":
            slug, action = parts[1], parts[2]
            if action == "lock":
                acct = self._require_account()
                b = self._body()
                p = app.market.confirm_lock(acct, slug, b.get("txid"), b.get("vout"))
                # The same shape the sale's own page returns, so a client that
                # has just locked can show the address and the leaves it can
                # rebuild without asking again.
                return self._json(200, app.market.project_detail(p.slug))
            if action == "withdraw":
                acct = self._require_account()
                app.market.withdraw(acct, slug)
                return self._json(200, {"withdrawn": slug})
            if action == "buy":
                acct = self._require_account()
                b = self._body()
                plan = app.market.plan_buy(acct, slug,
                                           token_atoms=b.get("token_atoms"),
                                           payment_atoms=b.get("payment_atoms"))
                plan["quote"] = app.rails.quote(b.get("rail") or R.USDX, plan["payment_atoms"])
                return self._json(200, plan)
            if action == "transaction":
                acct = self._require_account()
                b = self._body()
                built = app.market.build_buy(acct, slug, b, b.get("buyer") or {})
                return self._json(200, built)
            if action == "reclaim":
                acct = self._require_account()
                return self._json(200, app.market.build_reclaim(acct, slug, self._body()))
            if action == "flag":
                acct = self._require_account()
                b = self._body()
                p = app.market.set_visibility(acct, slug, hidden=b.get("hidden"),
                                              notice=b.get("notice"))
                return self._json(200, {"slug": p.slug, "hidden": p.hidden,
                                        "notice": p.notice,
                                        "reaches": "this page only: the sale is a "
                                                   "covenant on chain and can still "
                                                   "be bought from with its terms"})
            if action == "confirm":
                acct = self._require_account()
                b = self._body()
                recorded = app.market.record_purchase(
                    acct, slug, b.get("txid"), b.get("token_atoms"), b.get("payment_atoms"))
                return self._json(200, recorded)

        allowed = _methods_for(parts)
        if allowed and method not in allowed:
            # The path exists; the verb does not belong to it. Answering 404
            # tells a client its URL is wrong when only its method was.
            return self._json(405, {"error": "%s is not allowed here; this path "
                                             "takes %s" % (method, ", ".join(allowed))},
                              headers={"Allow": ", ".join(allowed + ["OPTIONS"])})
        return self._json(404, {"error": "no such endpoint"})

    # --- the SPA ------------------------------------------------------------

    def _static(self, path):
        root = self.app.webroot
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return self._json(403, {"error": "forbidden"})
        if not target.is_file():
            if rel.startswith("assets/") or rel.startswith("fonts/") \
                    or (target.suffix and target.suffix != ".html"):
                # A file that is not there is not the app: a stale bundle
                # asking for an old hashed asset should hear 404 and reload,
                # not receive HTML as a script.
                return self._json(404, {"error": "no such file"})
            target = root / "index.html"      # history-API fallback
        if not target.is_file():
            return self._json(404, {"error": "the Levo web app is not built; "
                                             "run npm run build in web/"})
        body = target.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
            ".json": "application/json",
            ".png": "image/png",
            ".woff2": "font/woff2",
            ".webmanifest": "application/manifest+json",
        }.get(target.suffix, "application/octet-stream")
        if target.suffix in (".js", ".css", ".woff2") and ("/assets/" in path or "/fonts/" in path):
            cache = "public, max-age=31536000, immutable"
        else:
            cache = "no-cache"
        self._send(200, body, ctype, cache=cache)


class Unauthorised(Exception):
    pass


class Unsupported(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _check_pubkey(pk):
    if len(pk) != 66 or not pk.startswith(("02", "03")) \
            or any(c not in "0123456789abcdef" for c in pk):
        raise ValueError("staker_pubkey must be a 33-byte compressed key in hex")


# systemd reads a <N> prefix on a line as its syslog priority, so an operator's
# `journalctl -p err` finds what went wrong instead of scrolling every request.
# One prefix per line: systemd parses them line by line, and a traceback is
# many lines.
def _log_error(message):
    for line in str(message).rstrip().splitlines() or [""]:
        sys.stderr.write("<3>levod %s\n" % line)


def _log_warning(message):
    for line in str(message).rstrip().splitlines() or [""]:
        sys.stderr.write("<4>levod %s\n" % line)


def _log_notice(message):
    sys.stderr.write("<5>levod %s\n" % str(message).rstrip())


def _accounts(text):
    """Account keys from an environment list: comma or space separated, each a
    33-byte compressed public key in hex. Anything else is dropped with a note
    on stderr rather than silently granting or silently refusing."""
    out = []
    for part in re.split(r"[,\s]+", str(text or "").strip()):
        if not part:
            continue
        key = part.strip().lower()
        if re.match(r"^0[23][0-9a-f]{64}$", key):
            out.append(key)
        else:
            _log_warning("LEVOD_OPERATORS entry %r is not a compressed public "
                         "key; ignored" % part)
    return out


# What each API path takes. One table, read by OPTIONS and by the fall-through,
# so a wrong verb is answered as a wrong verb and OPTIONS does not advertise
# methods a path has never had.
API_METHODS = (
    (("health",), ["GET"]),
    (("config",), ["GET"]),
    (("tiers",), ["GET"]),
    (("rails",), ["GET"]),
    (("watcher",), ["GET"]),
    (("me",), ["GET"]),
    (("me", "projects"), ["GET"]),
    (("me", "positions"), ["GET"]),
    (("auth", "challenge"), ["POST"]),
    (("auth", "verify"), ["POST"]),
    (("stake", "challenge"), ["POST"]),
    (("stake", "link"), ["POST"]),
    (("stake", "unlink"), ["POST"]),
    (("outputs", "check"), ["POST"]),
    (("projects",), ["GET", "POST"]),
    (("projects", "*"), ["GET", "PATCH", "DELETE"]),
    (("projects", "*", "lock"), ["POST"]),
    (("projects", "*", "buy"), ["POST"]),
    (("projects", "*", "transaction"), ["POST"]),
    (("projects", "*", "confirm"), ["POST"]),
    (("projects", "*", "reclaim"), ["POST"]),
    (("projects", "*", "withdraw"), ["POST"]),
    (("projects", "*", "flag"), ["POST"]),
    (("projects", "*", "fee"), ["GET"]),
    (("projects", "*", "purchases"), ["GET"]),
)


def _methods_for(parts):
    """The methods an API path takes, or None when there is no such path."""
    for shape, methods in API_METHODS:
        if len(shape) != len(parts):
            continue
        if all(a == "*" or a == b for a, b in zip(shape, parts)):
            return list(methods)
    return None


def _one(query, name):
    """One value for a query parameter, or None. A repeated parameter is a
    client bug, not a list: the first is taken and the rest ignored."""
    v = (query.get(name) or [None])[0]
    v = (v or "").strip()
    return v or None


def _int_param(query, name):
    v = _one(query, name)
    if v is None:
        return None
    if not v.lstrip("-").isdigit():
        raise M.PlatformError("%s must be a whole number" % name)
    return int(v)


def _str(body, name):
    v = body.get(name)
    if not isinstance(v, str) or not v.strip():
        raise ValueError("%s is required" % name)
    return v


def _fields_of(message):
    """The `Label: value` lines of a signed statement.

    Levo's statements are written to be read by the person signing them, so
    their fields are the contract. Reading the fields back out is what lets a
    signature be checked against what it actually says.
    """
    out = {}
    for line in str(message).splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


def _describe(e):
    if isinstance(e, KeyError):
        return "missing field %s" % e
    return str(e) or e.__class__.__name__


def _seq(atoms, label):
    import units as U
    return U.fmt(atoms, 8, label)


def _site_links(raw):
    """Where the rest of this deployment lives (wallet, faucet, staking pools),
    as JSON in LEVOD_LINKS. Only http(s) URLs are kept."""
    if not raw:
        return {}
    try:
        return M.validate_links(json.loads(raw))
    except (ValueError, M.PlatformError) as e:
        _log_warning("LEVOD_LINKS ignored: %s" % e)
        return {}


def main():
    host = os.environ.get("LEVOD_HOST", "127.0.0.1")
    port = int(os.environ.get("LEVOD_PORT", "8099"))
    Handler.app = App()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    Handler.app.watcher.start()
    sys.stderr.write("levod listening on http://%s:%d\n" % (host, port))
    sys.stderr.write("  chain %s, webroot %s\n" % (Handler.app.chain or "unknown", Handler.app.webroot))
    sys.stderr.write("  watching the chain every %ds\n" % Handler.app.watcher.interval)

    def stop(signum, frame):
        # systemd stops with SIGTERM. Let a save in flight finish, stop the
        # watcher, and close the listener, rather than dying mid-write.
        Handler.app.watcher.stop()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with Handler.app.market.lock:
            pass                        # wait for any mutation to finish
        srv.server_close()


if __name__ == "__main__":
    main()
