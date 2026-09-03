"""What levod says about the app it is serving.

levod serves a built SPA out of web/dist, and a build that fails leaves the
previous bundle exactly where it was. The pages render, every API check passes,
and the site is however many commits behind with nothing anywhere saying so.
These checks cover the facts health reports so that a deploy can refuse.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server as SV  # noqa: E402


class App:
    """Only the two attributes _bundle_state reads."""

    def __init__(self, webroot):
        self.webroot = Path(webroot)


def _site(index_html='<script src="/assets/index-abc123.js"></script>', src=None):
    root = Path(tempfile.mkdtemp(prefix="levo-bundle-"))
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(index_html, encoding="utf-8")
    if src is not None:
        (root / "src").mkdir()
        for name, text in src.items():
            (root / "src" / name).write_text(text, encoding="utf-8")
    return App(dist)


def test_no_bundle_is_no_claim(t):
    root = Path(tempfile.mkdtemp(prefix="levo-bundle-"))
    (root / "dist").mkdir()
    t.eq(SV._bundle_state(App(root / "dist")), {}, "an empty webroot reports nothing")
    t.eq(SV._bundle_state(App(root / "nowhere")), {}, "and neither does a missing one")


def test_the_bundle_is_named_and_dated(t):
    app = _site()
    state = SV._bundle_state(app)
    t.eq(state["bundle"], "assets/index-abc123.js", "the entry file identifies the bundle")
    t.ok(abs(state["built_at"] - time.time()) < 60, "with the time it was written")
    t.ok("source_newer_than_bundle" not in state,
         "and no staleness claim where there is no source to compare with")


def test_a_source_older_than_the_bundle_is_current(t):
    app = _site(src={"App.jsx": "// built already\n"})
    old = time.time() - 600
    os.utime(app.webroot.parent / "src" / "App.jsx", (old, old))
    state = SV._bundle_state(app)
    t.eq(state["source_newer_than_bundle"], False, "a bundle newer than its source is current")


def test_a_source_newer_than_the_bundle_is_stale(t):
    """The shape of a deploy whose build failed.

    The checkout moved to the new commit, so every file under src is newer than
    the bundle beside it; the bundle is the one built before the pull. Nothing
    else about the site changes, which is exactly why it has to be said here.
    """
    app = _site(src={"App.jsx": "// the new commit\n"})
    old = time.time() - 600
    os.utime(app.webroot / "index.html", (old, old))
    state = SV._bundle_state(app)
    t.eq(state["source_newer_than_bundle"], True, "a source newer than the bundle is stale")
    t.eq(state["bundle"], "assets/index-abc123.js", "and the stale bundle is still named")


def test_a_nested_source_file_counts(t):
    """A change one directory down is still a change.

    The pages live in src/pages and the components in src/components, so a walk
    that only read the top of the tree would call almost every real deploy
    current.
    """
    app = _site(src={"App.jsx": "// old\n"})
    old = time.time() - 600
    os.utime(app.webroot / "index.html", (old, old))
    os.utime(app.webroot.parent / "src" / "App.jsx", (old - 60, old - 60))
    t.eq(SV._bundle_state(app)["source_newer_than_bundle"], False, "all older: current")
    pages = app.webroot.parent / "src" / "pages"
    pages.mkdir()
    (pages / "Home.jsx").write_text("// the new commit\n", encoding="utf-8")
    t.eq(SV._bundle_state(app)["source_newer_than_bundle"], True,
         "a file a directory down is seen")


def test_an_unreadable_index_still_reports_what_it_can(t):
    """A bundle whose index cannot be parsed is still a bundle.

    The name is a convenience for telling two deployments apart. Losing it is
    not a reason to lose the staleness answer, which is the part a deploy acts
    on.
    """
    app = _site(index_html="<html>no script tag here</html>")
    state = SV._bundle_state(app)
    t.ok("built_at" in state, "the date survives an index with no entry script")
    t.ok("bundle" not in state, "and the name is simply absent rather than invented")
