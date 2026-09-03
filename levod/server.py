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
import socket
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
# The whole of one request -- its line, its headers and its body -- against one
# wall clock. A socket timeout re-arms on every read, so on its own it bounds
# nothing: a client that sends a byte every few seconds holds a handler slot
# for as long as it likes.
# The default, which `_read_settings()` replaces with the operator's figure
# when the service starts. It is a number rather than None because the drills
# and the demo build a Handler without going through main().
REQUEST_DEADLINE = 20.0


def _setting(name, default, kind=int, least=None):
    """A number from the environment, or a refusal naming the setting.

    A mistyped setting is not a thing to guess at. Reading it as the default
    runs a deployment on figures its operator did not choose and cannot see;
    letting it raise gives systemd a traceback and a restart loop over a typo
    it will meet again every five seconds. So it says which setting, what was
    in it, and stops with the status that means restarting will not help.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except (TypeError, ValueError):
        _refuse_setting(name, raw, "a number")
    if least is not None and value < least:
        _refuse_setting(name, raw, "at least %s" % least)
    return value


def _refuse_setting(name, raw, wanted):
    sys.stderr.write("<3>levod: %s is %r; it has to be %s. levod is not "
                     "starting: a setting it cannot read is a deployment "
                     "running on figures nobody chose.\n" % (name, raw, wanted))
    raise SystemExit(ST.BAD_STATE_EXIT)
# What /api/health will name, and how much of an error it will quote. A health
# answer is a fixed-size fact about the service, not a report that grows with
# the platform: whatever is watching it reads it every few seconds.
HEALTH_LIST = 20
HEALTH_ERROR = 500
WATCHER_RPC_TIMEOUT = 300         # a UTXO-set scan on a big chain takes a while
# The chains whose tokens are worth something. Everything else is a test
# chain, including one this build has never heard of.
MAINNET_CHAINS = ("sequentia", "main")

BUSY_WAIT_SECONDS = 5            # how long a request waits for a free handler
MAX_HANDLERS = 64                 # concurrent requests before the rest wait
# Polls that must fail in a row before health calls the watcher broken. One is
# a bad minute on the node; three in a row is something to look at.
WATCHER_FAILURES_BEFORE_UNHEALTHY = 3


# `str.isdigit()` is true of a superscript, of another script's digits,
# and of a string int() then refuses; it is a question about characters,
# not about numbers. The numbers here are amounts, so the gate is the
# shape of a written integer and nothing else.
WHOLE_NUMBER = re.compile(r"^-?[0-9]+$")


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
        # How long the node's tip may be reused. Two seconds is nothing against
        # a chain with block times, and it turns three round trips per page
        # into one. A chain that mines on demand -- a regtest, a test rig --
        # wants none of it, because there a block can arrive and be acted on in
        # the same second.
        ttl = os.environ.get("LEVOD_CHAIN_TTL")
        if ttl is not None and hasattr(self.node, "chain_info_ttl"):
            try:
                self.node.chain_info_ttl = float(ttl)
            except ValueError:
                _log_warning("LEVOD_CHAIN_TTL is not a number; leaving it at "
                             "%s seconds" % getattr(self.node, "chain_info_ttl", 0))
        self.links = T.StakeLinks()
        self.payment_decimals = _payment_decimals()
        try:
            table = T.tiers_from_env(payment_decimals=self.payment_decimals)
        except ValueError as e:
            # A tier table is the most hand-edited setting there is, and the
            # tiers are what every cap on the site is measured with: running on
            # the defaults instead would be a deployment quoting figures its
            # operator never chose.
            sys.stderr.write("<3>levod: %s. levod is not starting.\n" % str(e).rstrip("."))
            raise SystemExit(ST.BAD_STATE_EXIT)
        self.policy = T.TierPolicy(table, payment_decimals=self.payment_decimals)
        self.reader = T.StakeReader(self.node, self.links, self.policy,
                                    floor=lambda: self.staking_floor)
        # The service takes the state file for itself: two levods on one file
        # overwrite each other's ledgers, and each believes it wrote what it
        # holds.
        self.store = ST.Store(exclusive=True)
        payment_asset = os.environ.get("LEVOD_PAYMENT_ASSET", M.DEFAULT_PAYMENT_ASSET).lower()
        if not M.ASSET_RE.match(payment_asset):
            # Every sale is priced in this. A typo here first reaches a PROJECT
            # as "payment_asset is not 32 bytes" when they try to list, and
            # never reaches the operator at all.
            raise SystemExit(
                "levod: LEVOD_PAYMENT_ASSET is %s. Set it to the 64-character "
                "id of the asset this Levo prices sales in -- every price, cap "
                "and quote is denominated in it, so there is no default worth "
                "guessing." % ("not set" if not payment_asset
                               else "%r, which is not a 64-character asset id"
                                    % payment_asset))
        payment_label = os.environ.get("LEVOD_PAYMENT_LABEL", "USDX")

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
        self._floor = None
        self._floor_from_chain = False
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
                                 or R.BTC_RATE_LABEL),
                             payment_decimals=self.payment_decimals)
        self.watcher = None
        self.operators = _accounts(os.environ.get("LEVOD_OPERATORS"))
        # An asset registry to check a listing's claims against, if this
        # deployment has one.
        self.registry_url = (os.environ.get("LEVOD_REGISTRY_URL") or "").rstrip("/") or None
        self.market = M.Platform(self.store, self.reader, self.rails, self.node,
                                 hrp=lambda: self.hrp, payment_asset=payment_asset,
                                 payment_label=payment_label,
                                 stake_label=self.stake_label,
                                 payment_decimals=self.payment_decimals,
                                 operators=self.operators,
                                 registry_url=self.registry_url,
                                 on_stale=lambda: self.watcher and self.watcher.nudge())
        watch_node = self.node.with_timeout(WATCHER_RPC_TIMEOUT) \
            if hasattr(self.node, "with_timeout") else self.node
        self.watcher = W.Watcher(
            self.market, watch_node,
            interval=_setting("LEVOD_WATCH_SECONDS", 60, int, least=1),
            hrp=lambda: self.hrp, log=_log_error, note=_log_notice)
        self.challenges = A.Challenges(site="Levo")
        self.stake_challenges = A.Challenges(site="Levo")
        self.sessions = A.Sessions()
        self.auth_limit = RateLimit(per_minute=_setting("LEVOD_AUTH_PER_MINUTE", 30, int, least=1))
        self.write_limit = RateLimit(per_minute=_setting("LEVOD_WRITES_PER_MINUTE", 120, int, least=1))
        # Reads are cheap per request and not free: the board asks the node for
        # its tip, and the fee route asks for the mempool and the rate table.
        # One caller in a loop could fill every handler with requests blocked
        # on the node while real visitors wait behind them.
        self.read_limit = RateLimit(per_minute=_setting("LEVOD_READS_PER_MINUTE", 600, int, least=1))
        self.handlers = threading.BoundedSemaphore(MAX_HANDLERS)
        # How many of those slots ONE address may hold at once, when that
        # address is not a proxy this Levo believes. Sixty-four sockets from a
        # single client is what makes a slow-request attack cost a few bytes;
        # a reverse proxy is exempt because every request behind it arrives
        # from the same address, and capping it would cap the whole site.
        self.per_peer = _setting("LEVOD_PER_PEER", 8, int, least=0)
        self._peers = {}
        self._peers_lock = threading.Lock()
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
        # Whether this levod is meant to serve the app at all. An API-only run
        # is a deliberate configuration, not something to be inferred from what
        # happened to be on disk at startup: inferring it disarmed the check in
        # exactly the case it was written for -- a restart, a rebuild or a
        # restore that left the app missing -- and health then reported a
        # healthy site that answered 404 for every page.
        self.api_only = (os.environ.get("LEVOD_API_ONLY") or "").strip().lower() \
            in ("1", "true", "yes", "on")
        self.verbose = bool(os.environ.get("LEVOD_VERBOSE"))

    @property
    def staking_floor(self):
        """The weight the CHAIN requires before a staker counts at all.

        Asked of the node, because it is not the same everywhere: a custom
        chain sets it and a regtest leaves it at zero, and the interface
        presents this number as a fact about consensus rather than about Levo.
        Falls back to the mainnet constant, and says which it is using so the
        copy can stop asserting what it cannot check.
        """
        if self._floor is None:
            try:
                self._floor = self.node.staking_floor()
            except Exception:
                self._floor = None
            self._floor_from_chain = self._floor is not None
        return T.POS_MIN_STAKE_ATOMS if self._floor is None else self._floor

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
            "staking_floor_atoms": self.staking_floor,
            "staking_floor_from_chain": self._floor_from_chain,
            "first_tier_atoms": first.min_stake_atoms,
            "first_tier_is_chain_floor": (self._floor_from_chain
                                         and first.min_stake_atoms == self.staking_floor),
            "links": self.site_links,
            "source_url": self.source_url,
            "registry_url": self.registry_url,
        }

    def tiers_note(self):
        first = self.policy.tiers[1] if len(self.policy.tiers) > 1 else None
        if first is None:
            return "Only staked Sequence counts, and only for keys you have proven you control."
        floor = self.staking_floor
        text = "The first tier begins at %s staked." % _seq(first.min_stake_atoms, self.stake_label)
        # Only the chain can say what the chain enforces. Where the node did
        # not answer, Levo says what its own table does and stops there.
        if not self._floor_from_chain:
            pass
        elif not floor:
            text += (" This chain sets no blocksigner floor, so the threshold "
                     "above is Levo's own.")
        elif first.min_stake_atoms == floor:
            text += (" That is the chain's own blocksigner floor: below it, "
                     "consensus ignores a staker's weight entirely.")
        else:
            text += (" The chain's own blocksigner floor is %s."
                     % _seq(floor, self.stake_label))
        return text + (" Only staked Sequence counts, and only for keys you have "
                       "proven you control.")


class _Deadline:
    """A request's own reads, against one wall-clock budget.

    Everything the stdlib reads for a request goes through `rfile`, so setting
    the socket's timeout to what is LEFT of the budget before each read makes
    the budget total rather than per read. Anything else on the file object is
    passed through untouched.
    """

    def __init__(self, rfile, sock, deadline):
        self._rfile, self._sock, self._deadline = rfile, sock, deadline

    def _arm(self):
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("the request took longer than levod waits for one")
        try:
            self._sock.settimeout(min(left, 30))
        except Exception:
            pass

    def read(self, *a, **k):
        self._arm()
        return self._rfile.read(*a, **k)

    def readline(self, *a, **k):
        self._arm()
        return self._rfile.readline(*a, **k)

    def peek(self, *a, **k):
        self._arm()
        return self._rfile.peek(*a, **k)

    def __getattr__(self, name):
        return getattr(self._rfile, name)


class Handler(BaseHTTPRequestHandler):
    server_version = "levod"
    sys_version = ""
    # Keep-alive. Every answer here carries an exact Content-Length (a 304
    # carries none and needs none), which is the whole prerequisite. Without
    # this the connection was torn down after every response, so a page load
    # cost one TCP connection per file it asked for.
    protocol_version = "HTTP/1.1"
    # How long one connection may take to send its request. A client that
    # opens a socket, sends a byte and stops is holding a handler slot for
    # exactly this long, and sixty-four of them hold every slot levod has.
    # Ten seconds is longer than any real client needs on a loopback or a LAN,
    # and a slow public link is the reverse proxy's problem, not levod's.
    timeout = 10                 # replaced at startup, see _read_settings()
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
        headers = getattr(self, "headers", None)
        host = ((headers.get("Host") if headers else "") or "").strip()[:100]
        if not host or any(c in host for c in " \r\n"):
            return None
        proto = "http"
        peer = self.client_address[0] if self.client_address else "?"
        if self.app and peer in self.app.trusted_proxies:
            proto = (headers.get("X-Forwarded-Proto") or "http").strip().lower()
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
        # `headers` exists only once a request line has PARSED. This is called
        # from the logger, which the stdlib calls while answering a malformed
        # request -- and reading it there raised, so the client got no answer
        # at all and the fault appeared as a traceback rather than a line.
        headers = getattr(self, "headers", None)
        if headers and self.app and peer in self.app.trusted_proxies:
            fwd = headers.get("X-Forwarded-For")
            if fwd:
                # The LAST entry, not the first. A proxy appends the address it
                # received the connection from; everything to the left of that
                # is whatever the client sent, and taking the leftmost let a
                # caller choose its own rate-limit bucket per request -- which
                # is the same as having no rate limit at all.
                return fwd.split(",")[-1].strip()[:64]
        return peer

    def _take_peer_slot(self):
        """Whether this peer may hold another handler slot."""
        app, peer = self.app, (self.client_address[0] if self.client_address else "?")
        if not app or not app.per_peer or peer in app.trusted_proxies:
            return True
        with app._peers_lock:
            if app._peers.get(peer, 0) >= app.per_peer:
                return False
            app._peers[peer] = app._peers.get(peer, 0) + 1
        self._peer_held = peer
        return True

    def _drop_peer_slot(self):
        peer = getattr(self, "_peer_held", None)
        if not peer or not self.app:
            return
        self._peer_held = None
        with self.app._peers_lock:
            left = self.app._peers.get(peer, 1) - 1
            if left > 0:
                self.app._peers[peer] = left
            else:
                self.app._peers.pop(peer, None)

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
        if not self._take_peer_slot():
            body = b'{"error": "too many connections from this address"}'
            try:
                self.wfile.write(b"HTTP/1.1 429 Too Many Requests\r\n"
                                 b"Content-Type: application/json\r\n"
                                 b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                                 b"Retry-After: 5\r\n"
                                 b"Connection: close\r\n\r\n" + body)
            except Exception:
                pass
            self.close_connection = True
            return
        acquired = self.app.handlers.acquire(timeout=BUSY_WAIT_SECONDS) if self.app else True
        if not acquired:
            self._drop_peer_slot()
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
            # One deadline for the whole request, head and body together.
            #
            # A socket timeout is per read, and it re-arms: a client sending
            # one byte every few seconds never trips it, and holds a handler
            # slot for as long as it cares to. This is the wall clock, and it
            # is the only thing a dribbling client cannot outlast.
            deadline = time.monotonic() + REQUEST_DEADLINE
            plain, self.rfile = self.rfile, _Deadline(self.rfile, self.connection, deadline)
            try:
                super().handle_one_request()
            except socket.timeout:
                self.close_connection = True
            finally:
                self.rfile = plain
                try:
                    self.connection.settimeout(self.timeout)
                except Exception:
                    pass
        finally:
            if self.app:
                self.app.handlers.release()
            self._drop_peer_slot()

    # --- plumbing -----------------------------------------------------------

    # A journal line is read in a terminal, so a request line that carries
    # control characters is a request that can write whatever it likes there:
    # escape sequences recolour, move the cursor and overwrite what is above.
    # The standard library escapes them and this override used to lose that.
    LOG_ESCAPES = {c: "\\x%02x" % c for c in list(range(0, 32)) + [127]}

    def log_message(self, fmt, *args):
        if not self.app or not self.app.verbose:
            # Health checks, tier tables and asset fetches say nothing when
            # they succeed; the journal is for what an operator acts on.
            #
            # `path` is only set once a request line has PARSED. A malformed
            # or over-long one is logged before that, and reading it here
            # raised AttributeError from inside the logger, which left the
            # client with no answer at all.
            path = urlparse(getattr(self, "path", "") or "").path
            code = str(args[1]) if len(args) > 1 else ""
            quiet = path in ("/api/health", "/api/tiers", "/api/rails", "/api/config") \
                or path.startswith("/assets/") or path.endswith((".svg", ".png", ".woff2"))
            if quiet and code.startswith("2"):
                return
        sys.stderr.write("levod %s %s\n"
                         % (self.client(), (fmt % args).translate(self.LOG_ESCAPES)))

    def log_error(self, fmt, *args):
        pass                            # the request line is logged once, above

    def send_error(self, code, message=None, explain=None):
        """A refusal the stdlib raises before any handler runs -- a request
        line that does not parse, a version this does not speak, a header block
        too large -- answered the way everything else here is answered.

        The default is an HTML page carrying the interpreter's own words, on a
        server that speaks JSON and never otherwise says which Python it is.
        """
        body = json.dumps({"error": str(message or "bad request")}).encode()
        try:
            self.send_response(code, message)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except Exception:
            pass                        # the client is already gone
        self.close_connection = True

    def _send(self, code, body, ctype, cache="no-store", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # A 304 has no body by definition, and announcing a length for one is
        # how a persistent connection loses its place.
        if code != 304:
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
        """The verbs THIS path takes, and the site's own headers with them.

        A blanket list is a lie a scanner or a cache will act on -- it said
        every path took POST, PATCH and DELETE, including a static file and a
        read-only endpoint, and contradicted what OPTIONS answered for the same
        URL a moment earlier. Going through the ordinary responder also means
        the answer carries nosniff and the frame and referrer policies, which
        this one wrote itself and left out.
        """
        path = urlparse(getattr(self, "path", "") or "").path
        parts = [p for p in path.split("/") if p]
        allowed = _methods_for(parts[1:]) if parts[:1] == ["api"] else None
        if allowed is None:
            allowed = [] if path.startswith("/api/") else ["GET", "HEAD"]
        # The same list OPTIONS gives for this URL, HEAD included where GET is.
        verbs = list(allowed)
        if "GET" in verbs and "HEAD" not in verbs:
            verbs.append("HEAD")
        self._json(405, {"code": "method_not_allowed",
                         "error": "%s is not allowed here%s"
                                  % (self.command,
                                     ("; this path takes " + ", ".join(allowed))
                                     if allowed else "")},
                   headers={"Allow": ", ".join(verbs + ["OPTIONS"]) if verbs
                            else "OPTIONS"})

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
            self._json(e.code, {"code": "rate_limited" if e.code == 429 else "refused",
                                "error": str(e)},
                       headers={"Retry-After": "60"} if e.code == 429 else None)
        except Unauthorised as e:
            self._json(401, {"code": "sign_in_required", "error": str(e)})
        except M.NotFound as e:
            self._json(404, {"code": "not_found", "error": str(e)})
        except M.NotAuthorised as e:
            self._json(403, {"code": "not_allowed", "error": str(e)})
        except S.CapExceeded as e:
            self._json(409, {"code": "cap_exceeded", "error": str(e),
                             "allowance_atoms": str(e.allowance_atoms),
                             "enforced_by": "levo"})
        except (M.PlatformError, S.SaleError, A.BadSignature, R.RailUnavailable,
                ValueError) as e:
            self._json(400, {"code": "refused", "error": str(e)})
        except KeyError as e:
            # A field the request should have carried and did not. The caller
            # is at fault and deserves to hear which field.
            #
            # AttributeError is deliberately NOT caught here: it is what a bug
            # in levod looks like, and answering one as "malformed request"
            # blamed the caller for the server's mistake and logged nothing at
            # all. TypeError stays, because body parsing still leans on it.
            # Logged like the TypeError arm below: a KeyError raised anywhere
            # but the body parser is a bug in levod, and answering it as a
            # malformed request with nothing in the journal leaves no trace of
            # a server fault the caller was blamed for.
            _log_error("KeyError on %s %s: %s" % (method, path, e))
            _log_error(traceback.format_exc())
            self._json(400, {"code": "malformed", 
                             "error": "malformed request: %s" % _describe(e)})
        except TypeError as e:
            _log_error("malformed request on %s %s: %s" % (method, path, e))
            _log_error(traceback.format_exc())
            self._json(400, {"code": "malformed", "error": "malformed request"})
        except RPC.RPCError as e:
            self._json(502, {"code": "node_unavailable",
                             "error": "the Sequentia node is unreachable or "
                                      "refused the query: %s" % e})
        except Exception:
            _log_error("unhandled error on %s %s" % (method, path))
            _log_error(traceback.format_exc())
            self._json(500, {"code": "internal", "error": "internal error"})

    def _api(self, method, path, query):
        app = self.app
        parts = [p for p in path.split("/") if p][1:]      # drop "api"

        # -- what this address may ask for -----------------------------------
        #
        # Before any handler, so that the endpoints answered earliest -- the
        # config, the tier table, the rails, the watcher -- are inside the
        # limit rather than outside it. Health is the one exemption: an uptime
        # check must never be the thing that trips a limit.
        if method in ("GET", "HEAD") and parts[:1] != ["health"] and \
                not app.read_limit.allow(self.client()):
            raise Unsupported(429, "too many requests from this address; slow down")

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
            try:
                serving = app.api_only or (app.webroot / "index.html").is_file()
            except OSError as e:
                # A webroot that cannot be read at all is not a healthy site,
                # and it is not a reason for the endpoint that says so to
                # answer with a traceback.
                serving, node = False, dict(node, webroot_error=str(e))
            ok = (node["reachable"] and not stale and not failing
                  and not write_error and serving)
            return self._json(200 if ok else 503, {
                "service": "levod", "ok": ok, "node": node,
                # No path here: nothing reads it, and an unauthenticated
                # endpoint should not name the filesystem it runs on.
                "app": {"serving": serving},
                "payment": {"asset": app.rails.payment_asset,
                            "label": app.rails.payment_label,
                            "decimals": app.payment_decimals},
                "state_file": {"writable": not write_error,
                               "unsaved_changes": bool(app.store.dirty),
                               "last_error": write_error},
                "watcher": {"running": bool(w._thread and w._thread.is_alive()),
                            "last_run_age_seconds": int(age) if age is not None else None,
                            "reconciling": not failing,
                            "consecutive_errors": w.consecutive_errors,
                            # Bounded, both of them. This endpoint is polled
                            # every few seconds by anything watching the site,
                            # and a platform with a thousand sales could
                            # otherwise answer it with a list of a thousand
                            # slugs and a paragraph per failing sale.
                            "unverified_sales": list(w.unverified)[:HEALTH_LIST],
                            "unverified_total": len(w.unverified),
                            "last_error": (w.last_error or "")[:HEALTH_ERROR] or None},
            })

        if method == "GET" and parts == ["config"]:
            # Labels, prefixes and links: the same answer for every visitor,
            # and the first thing every page asks for.
            return self._json(200, app.config(), cache="public, max-age=30")

        if method == "GET" and parts == ["tiers"]:
            return self._json(200, cache="public, max-age=30", payload={
                "tiers": app.policy.to_json(),
                "staking_floor_atoms": app.staking_floor,
                "staking_floor_from_chain": app._floor_from_chain,
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

        # -- auth -------------------------------------------------------------
        if method == "POST" and parts[:1] in (["auth"], ["stake"]) and \
                not app.auth_limit.allow(self.client()):
            raise Unsupported(429, "too many sign-in attempts from this address; "
                                   "wait a minute and try again")
        # Everything that writes or makes the node work, not only what is
        # under /projects: `POST /outputs/check` asks the node about up to
        # thirty-two outputs and was in no limit at all.
        if method in ("POST", "PATCH", "DELETE") and parts[:1] not in (["auth"], ["stake"]) and \
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
            if address is not None and len(str(address)) > 200:
                # An address is at most about ninety characters on any chain
                # here. A field this size is not an address, and the decoders
                # behind it are not free.
                raise A.BadSignature("that is not an address")
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
            standing = _wire_standing(app.reader.standing(acct))
            if app.links.dirty:
                # The mutation under the lock; the write outside it. `save`
                # takes the lock itself to build its snapshot, and the lock is
                # reentrant, so holding it here would put the serialise and the
                # fsync inside it -- with every other request behind them.
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
            return self._json(200, _wire_standing(app.reader.standing(acct)))

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
            return self._json(200, _wire_standing(app.reader.standing(acct)))

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
            return self._json(405, {"code": "method_not_allowed",
                                    "error": "%s is not allowed here; this path "
                                             "takes %s" % (method, ", ".join(allowed))},
                              headers={"Allow": ", ".join(allowed + ["OPTIONS"])})
        return self._json(404, {"code": "not_found", "error": "no such endpoint"})

    # --- the SPA ------------------------------------------------------------

    def _static(self, path):
        root = self.app.webroot
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return self._json(403, {"code": "not_allowed", "error": "forbidden"})
        if not target.is_file():
            if rel.startswith("assets/") or rel.startswith("fonts/") \
                    or (target.suffix and target.suffix != ".html"):
                # A file that is not there is not the app: a stale bundle
                # asking for an old hashed asset should hear 404 and reload,
                # not receive HTML as a script.
                return self._json(404, {"code": "not_found", "error": "no such file"})
            target = root / "index.html"      # history-API fallback
        if not target.is_file():
            return self._json(404, {"code": "not_found",
                                    "error": "the Levo web app is not built; "
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
        # `no-cache` means "ask me before reusing this", and a browser can only
        # ask with a validator. Without one, every reload of the app shell and
        # every icon came back in full, and the answer was always the same
        # bytes. The tag is the file's own size and modification time, which is
        # what changes when a build replaces it.
        st = target.stat()
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", ctype, cache=cache, headers={"ETag": etag})
        self._send(200, body, ctype, cache=cache, headers={"ETag": etag})


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


def _payment_decimals():
    """How many places the payment asset divides into.

    Every amount of it is displayed, typed and capped through this. Eight is
    what Elements issues by default and what this testnet's USDX uses; an asset
    with another precision would be quoted a hundred times wrong in both
    directions, and its tier caps a million times.
    """
    raw = os.environ.get("LEVOD_PAYMENT_DECIMALS", "8")
    try:
        value = int(raw)
    except ValueError:
        _log_warning("LEVOD_PAYMENT_DECIMALS is not a number; using 8")
        return 8
    if not 0 <= value <= 8:
        _log_warning("LEVOD_PAYMENT_DECIMALS is out of range; using 8")
        return 8
    return value


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
    if not WHOLE_NUMBER.match(v):
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


# A field name a caller can act on: short, and shaped like the keys the API
# documents. Anything else -- a nonce, a session id, an asset id that reached a
# dictionary lookup -- is echoed back to nobody, because a key that is not a
# field name is a value from inside levod, and the caller did not send it.
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _wire_standing(standing):
    """An account's standing, with its atom counts as decimal strings.

    `standing()` is read inside levod as well as sent out -- the tier a stake
    earns is decided by comparing it -- so it holds integers, and the
    conversion belongs here, at the edge, where a browser is the reader and a
    number above 2**53 arrives rounded.
    """
    out = dict(standing)
    for key in ("stake_atoms", "to_next_atoms"):
        if isinstance(out.get(key), int):
            out[key] = str(out[key])
    out["keys"] = [dict(k, weight_atoms=str(k["weight_atoms"]))
                   if isinstance(k.get("weight_atoms"), int) else k
                   for k in (out.get("keys") or [])]
    return out


def _describe(e):
    if isinstance(e, KeyError):
        key = e.args[0] if e.args else ""
        if isinstance(key, str) and FIELD_NAME.match(key):
            return "missing field %r" % key
        return "a field is missing or has the wrong shape"
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


def _read_settings():
    """The numbers that are read once, before anything is served."""
    global REQUEST_DEADLINE
    REQUEST_DEADLINE = _setting("LEVOD_REQUEST_DEADLINE", 20.0, float, least=1)
    Handler.timeout = _setting("LEVOD_CLIENT_TIMEOUT", 10.0, float, least=1)


def main():
    _read_settings()
    host = os.environ.get("LEVOD_HOST", "127.0.0.1")
    port = _setting("LEVOD_PORT", 8099, int, least=1)
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
