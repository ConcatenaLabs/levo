"""Reading a request body, and reading it twice.

The bytes of a request body exist once. A handler that asks for them a second
time -- which is what an ordinary refactor produces, a validation moved next to
the thing it validates -- used to block on a socket that would send no more
until the request deadline expired: twenty seconds of nothing, then no answer,
which reads as a hung server rather than as the one-line mistake it is.
"""

import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server as SV  # noqa: E402


class OnceOnly(io.BytesIO):
    """A body that can be read once, the way a socket behaves."""

    def __init__(self, data):
        super().__init__(data)
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        return super().read(n)


class Req:
    """Enough of the handler for _body(), and nothing else."""

    _body = SV.Handler._body
    _read_body = SV.Handler._read_body

    def __init__(self, body=b'{"a": 1}', headers=None):
        self.rfile = OnceOnly(body)
        self.headers = headers if headers is not None else {
            "Content-Length": str(len(body))}


def test_the_body_is_read_once(t):
    r = Req()
    first = r._body()
    t.eq(first, {"a": 1}, "the body parses")
    t.eq(r.rfile.reads, 1, "and the socket was read once")
    second = r._body()
    t.eq(second, {"a": 1}, "a second call gives the same body")
    t.eq(r.rfile.reads, 1, "without going back to a socket that has no more to give")
    t.ok(first is second, "and it is the same object, not a reparse")


def test_an_empty_body_is_cached_too(t):
    """The case a cache keyed on truthiness would miss.

    An empty body parses to {}, which is falsy: a cache that tested the stored
    value rather than its presence would read the socket again every time, and
    the endpoints that take no body are most of them.
    """
    r = Req(body=b"", headers={"Content-Length": "0"})
    t.eq(r._body(), {}, "no body is an empty one")
    t.eq(r._body(), {}, "twice")
    t.eq(r.rfile.reads, 0, "and a zero-length body is never read from the socket")


def test_a_refusal_is_not_cached_as_an_answer(t):
    """A body levod refused must not come back as {} on the next look.

    Both calls have to raise the same way, or a handler that catches the first
    refusal and asks again would proceed on an empty body it never received.
    """
    r = Req(body=b"not json", headers={"Content-Length": "8"})
    for attempt in ("first", "second"):
        try:
            r._body()
            t.ok(False, "the %s read refuses invalid JSON" % attempt)
        except ValueError as e:
            t.ok("not valid JSON" in str(e), "the %s read says why" % attempt, str(e))


def test_the_size_limit_still_bites(t):
    big = {"Content-Length": str(SV.MAX_BODY + 1)}
    try:
        Req(body=b"{}", headers=big)._body()
        t.ok(False, "a body over the limit is refused")
    except ValueError as e:
        t.ok("larger than" in str(e), "and says so", str(e))
