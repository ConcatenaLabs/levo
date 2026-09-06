"""The API document names every route the router has, and no other.

doc/api.md is written by hand, and the router's table is what answers. The two
drift apart one endpoint at a time and nobody notices, because a reader of the
document does not have the table open and a reader of the table does not have
the document. This walks both. It reads the document the way it is written --
an entry is **`METHOD /path`**, and a long one wraps after the method -- so it
does not have to be reformatted to be checked.
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server as SV  # noqa: E402

DOC = HERE.parent.parent / "doc" / "api.md"


def routes_in_the_router():
    out = set()
    for shape, methods in SV.API_METHODS:
        path = "/api/" + "/".join("<slug>" if s == "*" else s for s in shape)
        for m in methods:
            out.add((m, path))
    return out


def routes_in_the_doc():
    text = DOC.read_text(encoding="utf-8")
    # **`GET /api/x`** possibly wrapped as **`POST\n/api/x`**
    found = set()
    for m in re.finditer(r"\*\*`(GET|POST|PATCH|DELETE)\s+(/api/[^`\s]+)`\*\*", text):
        found.add((m.group(1), m.group(2)))
    return found


def test_every_route_is_documented(t):
    router, doc = routes_in_the_router(), routes_in_the_doc()
    t.ok(len(router) >= 25, "the router has its routes", len(router))
    missing = sorted(router - doc)
    t.eq(missing, [], "every route the router answers is in doc/api.md")


def test_the_doc_names_no_route_the_router_lacks(t):
    router, doc = routes_in_the_router(), routes_in_the_doc()
    extra = sorted(doc - router)
    t.eq(extra, [], "doc/api.md names no route the router does not have")
