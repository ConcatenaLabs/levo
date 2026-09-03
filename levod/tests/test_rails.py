"""Rail checks: quotes, availability, and the ways the rate table can fail."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import rails as R  # noqa: E402

USDX = "2a515539da5e6a60caa7766ecd65bac0c10d15717ddd2088844ba58f4d04b9de"


class Src:
    def __init__(self, rates):
        self.rates_ = rates
        self.calls = 0

    def call(self, method, *params):
        self.calls += 1
        return dict(self.rates_)


def _rails(rates, **kw):
    return R.Rails(USDX, "USDX", R.NodeRateSource(Src(rates), **kw))


def test_quotes_round_against_the_buyer(t):
    r = _rails({"SBTC": 6400000000000, "USDX": 100000000})
    q = r.quote("btc", 25 * 10**8)
    t.eq(q["send_sats"], 39063, "39062.5 sats rounds up")
    t.eq(q["delivers_atoms"], 25 * 10**8, "and delivers exactly what the covenant needs")
    t.ok(q["expires_at"] > q["taken_at"], "a BTC quote expires")
    t.ok("Levo takes no custody" in q["note"], "and says who holds what")


def test_rail_ids_are_case_insensitive(t):
    r = _rails({"SBTC": 6400000000000, "USDX": 100000000})
    for rail in ("BTC", "Btc", " btc "):
        t.eq(r.quote(rail, 1)["rail"], "btc", "%r is the BTC rail" % rail)
    for rail in ("USDX", "usdx", "", None):
        t.eq(r.quote(rail, 1)["rail"], "usdx", "%r is the USDX rail" % rail)
    try:
        r.quote("gold", 1)
        t.ok(False, "an unknown rail is refused")
    except R.RailUnavailable:
        t.ok(True, "an unknown rail is refused")


def test_the_btc_rail_is_unavailable_without_a_price(t):
    r = _rails({"USDX": 100000000})
    avail = {x["id"]: x for x in r.available()}
    t.eq(avail["btc"]["available"], False, "no bitcoin price: no BTC rail")
    t.ok(avail["btc"]["unavailable_because"], "with a reason")
    t.eq(avail["usdx"]["asset"], USDX, "the USDX rail names its asset id")
    try:
        r.quote("btc", 1)
        t.ok(False, "a BTC quote is refused")
    except R.RailUnavailable:
        t.ok(True, "a BTC quote is refused")


def test_a_rate_that_prices_below_one_atom_is_unavailable(t):
    r = _rails({"SBTC": 1, "USDX": 10**30})
    t.eq({x["id"]: x["available"] for x in r.available()}["btc"], False,
         "an absurd table makes the rail unavailable rather than dividing by zero")


def test_rates_are_cached_briefly(t):
    src = Src({"SBTC": 6400000000000, "USDX": 100000000})
    ns = R.NodeRateSource(src, ttl=30)
    ns.rates(); ns.rates()
    t.eq(src.calls, 1, "two reads within the TTL ask the node once")
    ns._at = 0
    ns.rates()
    t.eq(src.calls, 2, "and again once it is stale")


def test_the_swap_step_is_quoted_at_the_payment_asset_precision(t):
    """The swap step names an amount a buyer goes and obtains. On a two-place
    asset, printing it at eight places is wrong by a factor of a million."""
    r = R.Rails(USDX, "USDX", R.NodeRateSource(Src({"SBTC": 6_400_000_000_000,
                                                    "USDX": 100_000_000})),
                payment_decimals=2)
    q = r.quote("btc", 12_345)          # 123.45 USDX at two places
    t.ok("123.45 USDX" in q["steps"][0],
         "the swap step names the amount in the asset's own units", q["steps"][0])


def test_a_registry_answer_is_held_to_the_same_rule_as_a_lister(t):
    """A registry is another operator's HTTP service and its answer is printed
    on a Levo page beside the project's own words. A rule enforced only against
    the people who can be identified is not a rule."""
    import registry as REG
    a = REG.Answer(checked=True, found=True, contract={
        "ticker": "SB\u202eTC",
        "name": "Pegged Bitcoin\u200b",
        "entity": {"domain": "ok.test "}})
    t.eq(a.ticker, "SBTC", "a bidi override is dropped from a ticker")
    t.eq(a.name, "Pegged Bitcoin", "and an invisible character from a name")
    t.eq(a.domain, "ok.test", "and the whitespace around a domain")
    t.eq(REG.Answer(checked=True, found=True,
                    contract={"ticker": "\u200b\u200b"}).ticker, None,
         "a ticker of nothing but invisible characters is no ticker")
    t.eq(REG.Answer(checked=True, found=True,
                    contract={"name": "Ordinary Token"}).name, "Ordinary Token",
         "and ordinary text is untouched")


def test_a_registry_answer_is_read_with_an_end_to_it(t):
    """The registry is another operator's HTTP service, reached while a listing
    is being made. A body with no end to it would be levod's memory rather than
    theirs, and a listing that hangs on somebody else's server is a listing
    nobody can make."""
    import io
    import registry as REG

    class Endless:
        """A server that keeps answering."""
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=None):
            # However much is asked for, there is more.
            return b'{"ticker": "' + b'A' * ((n or 10 ** 7)) + b'"}'

    a = REG.look_up("https://registry.test", "aa" * 32,
                    opener=lambda url, timeout=None: Endless())
    t.ok(a.error and "larger than" in a.error,
         "a registry answering without end is refused, not read", a.error)
    t.eq(a.found, False, "and the listing is not held up by it")
